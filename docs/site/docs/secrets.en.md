# Secrets

Rayito can **store** secrets in your account's AWS Secrets Manager
(`SecretStore`, and the E2B shim's `Secret`/`AsyncSecret`) and **deliver them**
to a sandbox as environment variables (`secrets=`), with a cache that avoids
fetching the secret on every call (`SecretCache`).

<small>Since 0.5.0. Accepted against real AWS Secrets Manager.</small>

!!! info "Cost and activation"
    - **Default**: off. Without `secrets=` / `secret_cache=` (TypeScript:
      `secrets` / `secretCache`) and without instantiating `SecretStore`, Rayito does not
      build any Secrets Manager client, does not import
      `@aws-sdk/client-secrets-manager` and makes no call. No
      environment variable or configuration file turns it on.
    - **Turns on**: `SecretStore(...)` stores, updates, lists and deletes
      secrets under a prefix (`rayito/`); `secrets=` delivers their value as an
      environment variable of a command, a PTY, a cell or a context.
    - **Resources and AWS calls**: one Secrets Manager secret per
      `create`; `GetSecretValue` once per secret and cache TTL (300 s),
      never once per command.
    - **Approximate cost** (us-east-1, checked 2026-09-30,
      [pricing](https://aws.amazon.com/secrets-manager/pricing/)): $0.40 per
      secret per month **until you delete it**, plus $0.05 per 10,000 calls
      (≈ $0.04/month per secret and process with the default TTL).
    - **IAM** (credentials of whoever calls the SDK): `RayitoSecretsReader` to
      inject and `RayitoSecretsAdmin` for the CRUD, from
      `infra/secrets-access.yaml`.
    - **How to turn it off**: remove `secrets=` and stop instantiating `SecretStore`;
      `destroy()` the secrets you no longer use ([How to turn it off](#como-apagarlo)).

!!! warning "Phase 1: the sandbox code CAN read an injected secret"
    `secrets=` delivers the **value** as an environment variable of the process that
    asks for it. Any code running in the sandbox can read it: the environment of
    its own process, `/proc/<pid>/environ` of processes of the same uid
    (1000) and the **suspension memory snapshot** (if the sandbox
    is suspended with the process alive, the value ends up in that snapshot; whether AWS
    encrypts it at rest is the open question SEC-5). The first time a
    process uses `secrets=`, the SDK reminds you with a `RayitoCompatWarning`.

    With **untrusted code** (an agent's, a user's):

    - do not inject long-lived credentials (IAM access keys,
      personal tokens with no expiry);
    - inject **short-lived, minimal-scope** tokens (for example, a
      token for an API with read-only permission that expires in an hour);
    - use it only in processes you control (the specific command that
      needs it, not the whole shell), or
    - use the [secret gateway](funciones-opcionales/pasarela-de-secretos.md)
      (`gateways=`, since 0.6.0): the sandbox uses the credential against a
      fixed `upstream` without being able to read its value.

## What it turns on and what it costs { #que-activa-y-que-cuesta }

| | `SecretStore` (CRUD) | `secrets=` (injection) |
|---|---|---|
| Turns on | create, update, describe, list and delete secrets under a prefix (`rayito/` by default) | deliver the value as an environment variable of a command, a PTY, a Python cell or a code context |
| AWS resources | one Secrets Manager secret per `create` | none new |
| AWS calls | `CreateSecret`, `PutSecretValue`, `UpdateSecret`, `DescribeSecret`, `ListSecrets`, `DeleteSecret` (one per method; `update` and `destroy` first do a `DescribeSecret`) | `GetSecretValue` once per secret and cache TTL (300 s by default) |
| Cost (us-east-1, [pricing](https://aws.amazon.com/secrets-manager/pricing/), checked 2026-09-30) | **$0.40 per secret per month**, prorated, **until you delete it**, + $0.05 per 10,000 calls | $0.05 per 10,000 calls: with the default TTL, ≤ 12 reads/hour per secret and process ≈ **$0.04/month** |
| IAM (**caller's** credentials, not the execution role) | policy `RayitoSecretsAdmin` | policy `RayitoSecretsReader` |

!!! danger "AWS SDK v3 DEBUG logging prints the value"
    Rayito **never** writes a secret's value or name to its logs
    (`rayito.*` loggers in Python, the options' `logger` in
    TypeScript), to `repr`/`toJSON` or to errors.

    In Python, since 0.17.1 `import rayito` drops the DEBUG records of
    `botocore.endpoint`, `botocore.parsers` and `botocore.auth`, the
    botocore loggers that write request and response bodies: with
    `logging.DEBUG` on the root logger or on `botocore`, or with
    `boto3.set_stream_logger()`, the `SecretString` no longer shows up
    (before, it was printed in the clear for `CreateSecret`,
    `PutSecretValue` and `GetSecretValue`). Their warnings and errors, and
    the rest of botocore's DEBUG, still come through. The agent host does
    the same inside the MicroVM.

    In TypeScript, a `logger` you pass yourself to the SDK v3
    `SecretsManagerClient` **does** receive those bodies: do not set one in
    processes that handle secrets.

Exact contract of parameters and errors: `AWS_API_NOTES.md` §19 (on
[GitHub](https://github.com/alejandro-cedeno-10/rayito/blob/main/AWS_API_NOTES.md)). A KMS
CMK (`kms_key_id=`) adds KMS cost and the `kms:Decrypt` /
`kms:GenerateDataKey` permissions; the AWS-managed key (`aws/secretsmanager`) does not.

## IAM: the optional template { #iam-la-plantilla-opcional }

`infra/secrets-access.yaml` creates two managed IAM policies and nothing else
(cost $0): `RayitoSecretsReader` (`GetSecretValue`, `DescribeSecret` on
`arn:aws:secretsmanager:<region>:<account>:secret:rayito/*`) and
`RayitoSecretsAdmin` (in addition `CreateSecret`, `PutSecretValue`,
`UpdateSecret`, `DeleteSecret` on that ARN and `ListSecrets` on `*`;
`destroy` uses `DescribeSecret` and `DeleteSecret`, both included). With
`KmsKeyArn`, it adds `kms:Decrypt` (and `kms:GenerateDataKey` in the
admin one) only through Secrets Manager (`kms:ViaService`).
`RayitoSecretsReader` also **denies** `GetSecretValue` on
`rayito/webhooks/*`: that is where the signing secrets of the
[webhooks](funciones-opcionales/eventos-y-webhooks.md) live, and a sandbox that
received one through `secrets=` could forge signed deliveries. The SDK
also refuses to read them through `secrets=`/`SecretCache`
(`InvalidArgumentException`) even if your policy allowed it.
`SecretPrefix` must end in `/`: `rayito` (without a slash) would also grant
other people's secrets such as `rayito-prod-db`. It is deployed as the
`secrets-access` component of [`rayito stack`](funciones-opcionales/pilas-opcionales.md):

```bash
rayito stack deploy secrets-access --param SecretPrefix=rayito/
# with your own KMS key (SecretStore(kms_key_id=...)), add:
#   --param KmsKeyArn=arn:aws:kms:<region>:123456789012:key/<id>
rayito stack status secrets-access    # outputs ReaderPolicyArn and AdminPolicyArn
```

Assign `ReaderPolicyArn` (or `AdminPolicyArn` if you are going to create and delete
secrets) to whoever runs the SDK, just like the policy of
[Set up AWS](primeros-pasos/configurar-aws.md#3-la-pila-de-iam):
`aws iam attach-user-policy` or `aws iam attach-role-policy`. `SecretPrefix`
must match `SecretStore(prefix=)`. To delete it:
`rayito stack destroy secrets-access` (removes the policies, not the
secrets). From the SDK, `OptionalStacks().deploy("secrets-access", ...)`
does the same. If you prefer CloudFormation by hand, the template is
`infra/secrets-access.yaml`; more detail in
[`infra/README.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/infra/README.md#secretos-infrasecrets-accessyaml-m13a).

## Examples { #ejemplos }

=== "Python"

    ```python
    import os

    from rayito import Sandbox, SecretCache, SecretRef, SecretStore

    store = SecretStore(region="us-east-1")                 # does not call AWS
    store.create("openai", os.environ["OPENAI_API_KEY"], metadata={"team": "ml"})

    cache = SecretCache(ttl_seconds=300, region="us-east-1")  # optional
    sbx = Sandbox.create(secrets={"OPENAI_API_KEY": "openai"}, secret_cache=cache)
    sbx.commands.run("python agent.py")        # 0 calls: resolved on create
    sbx.commands.run("python eval.py")         # 0 calls: cache hit
    sbx.commands.run("env", secrets={"GH_TOKEN": SecretRef("gh", version_stage="AWSCURRENT")})
    sbx.kill()

    store.update("openai", os.environ["OPENAI_API_KEY_NEW"])  # version 2
    cache.refresh("openai")                    # without waiting for the TTL
    store.destroy("openai")                    # stops billing
    ```

=== "TypeScript"

    ```ts
    import { Sandbox, SecretCache, SecretRef, SecretStore } from "rayito";

    const store = new SecretStore({ region: "us-east-1" }); // does not call AWS
    await store.create("openai", process.env.OPENAI_API_KEY!, { metadata: { team: "ml" } });

    const secretCache = new SecretCache({ ttlSeconds: 300, region: "us-east-1" });
    const sbx = await Sandbox.create({ secrets: { OPENAI_API_KEY: "openai" }, secretCache });
    await sbx.commands.run("python agent.py"); // 0 calls: resolved on create
    await sbx.commands.run("env", {
      secrets: { GH_TOKEN: new SecretRef("gh", { versionStage: "AWSCURRENT" }) },
    });
    await sbx.kill();

    await store.update("openai", process.env.OPENAI_API_KEY_NEW ?? "");
    await secretCache.refresh("openai");
    await store.destroy("openai");
    ```

    `@aws-sdk/client-secrets-manager` is an **optional peerDependency**:
    install it (`npm install @aws-sdk/client-secrets-manager`) only if you use
    secrets. Without it, nothing changes; with `secrets` or `SecretStore` and without
    it, the first call raises `InvalidArgumentError` with the install
    command.

=== "Shim E2B"

    ```python
    import os

    from rayito.e2b import Secret

    info = Secret.create("openai-key", os.environ["OPENAI_API_KEY"], region="us-east-1")
    print(info.secret_id)                 # the Secrets Manager ARN
    Secret.fill("openai-key")             # '${e2b.secrets.openai-key}': NOT resolved
    Secret.destroy("openai-key")
    ```

    The shim **lowercases** names (like E2B):
    `Secret.create("OpenAI", …)` stores `rayito/openai`. The native SDK's `secrets=` and
    `SecretStore` do not normalize, so that secret is
    injected as `secrets={"OPENAI_API_KEY": "openai"}`; with `"OpenAI"` the
    call raises `SecretNotFoundException`.

    The differences from E2B are in
    [E2B compatibility](e2b-compat.md#secretos-secret-asyncsecret).

## Where `secrets=` applies { #donde-se-aplica-secrets }

| Call | Python | TypeScript | Notes |
|---|---|---|---|
| Create / connect | `Sandbox.create(secrets=, secret_cache=)`, `Sandbox.connect(id, secrets=, …)`, `sbx.connect(secrets=, …)` | `Sandbox.create({ secrets, secretCache })`, `Sandbox.connect(id, { secrets })`, `sbx.connect({ secrets })` | the handle stores only the references; they are resolved **before** `run-microvm`: a missing secret fails without launching a VM; `connect` without `secrets` keeps the handle's (a `secret_cache=`/`secretCache` alone only changes the cache, not the references); `secrets={}` (TS: `secrets: {}`) clears them all and keeps the cache |
| Pool | `pool.take(secrets=)`, `Sandbox.create(pool=, secrets=)` | `pool.take({ secrets })`, `Sandbox.create({ pool, secrets })` | they are bound to the sandbox that comes out of the pool; warm slots never carry them (neither at launch nor in their `SlotRecord`) |
| Commands | `commands.run(..., secrets=)` (also `background=True`) | `commands.run(cmd, { secrets })` | `StartRequest.envs` |
| PTY | `pty.create(secrets=)` | `pty.create({ secrets })` | `PtyStart.envs` |
| Code | `run_code(..., secrets=)` | `runCode(code, { secrets })` | `ExecuteRequest.envs`, only in **Python** contexts and only during that cell; with another `language` it is `InvalidArgumentException`, and the handle's are not added to cells of other languages. With `context=` given as an **id** (text), the handle's only go if the context is `default` or one that handle created or listed as Python: an unknown id (created from another handle) goes without them; pass the `CodeContext` or `secrets=` in the call |
| Contexts | `create_code_context(secrets=)` | `createCodeContext({ secrets })` | `CreateContextRequest.envs`: the kernel's environment, for any language, while it lives |
| `reincarnate()` | relaunches with the references (and the cache) the handle has **at that moment**, including those from `pool.take(secrets=)` or `connect(secrets=)` | same | references only, never values |

Rules:

- **Handle ∪ call**: each call injects the handle's secrets plus its
  own; if they repeat a variable, the call wins.
- **All or nothing**: a variable that is in both `envs` and `secrets` is
  `InvalidArgumentException` (TS: `InvalidArgumentError`), never silently
  overwritten. The error names the variable, never the secret.
- A plain string is `SecretRef(name)`; `SecretRef(name,
  version_id=…)` or `version_stage=…` pins the version; a full ARN is used
  as is.
- They **never** travel in the `runHookPayload` (`create(envs=)` does not change),
  `metadata`, tags, image environment variables, pool records, logs,
  `repr`/`JSON.stringify` or error messages.
- Delivery reuses the `envs` of `ProcessService`, `PtyService` and
  `CodeService`, which already travel encrypted to the AWS proxy and authenticated
  with the access token; `rayd` builds the child's environment from scratch and does not
  log `envs`. There is no new RPC: it works with 0.4.0 images.

### For a deployed agent { #agente-desplegado }

An agent of `rayito agent deploy` (on MicroVMs or on AgentCore) receives
Secrets Manager values as environment variables with `[secret_env]` and
`[[secret_env_from]]` in its project file, or with `--secret-env` and
`--secret-env-from`. There the names are **full** (without the `rayito/`
prefix, like `--readable-secret`) and a key of a JSON secret is chosen with
`#key`. See [Secrets as environment variables](guias/secretos-como-entorno.md).

## The cache: never on every call { #la-cache-nunca-en-cada-llamada }

`SecretCache(ttl_seconds=300)` (TS: `new SecretCache({ ttlSeconds: 300 })`):

- **Key**: (region, credentials —the boto3 session in Python, the credentials
  provider in TypeScript—, resolved `SecretId`, `VersionId` or
  `VersionStage`, `AWSCURRENT` by default). Two accounts or two regions
  never share a value.
- **TTL** from 1 to 86,400 s (300 by default). `0` is rejected: it would be fetching the
  secret on every call.
- **A hit makes 0 AWS calls.** Only one request in flight per key:
  ten threads, tasks or promises at once make **one** `GetSecretValue`.
- **Refresh** only when the TTL expires or with `refresh(name)` (reads now) /
  `invalidate(name)` (discards; without a name, everything). After a
  `SecretStore.update`, readers see the new value when their TTL expires or
  with `refresh()`.
- **A secret that does not exist is not stored**: the next attempt asks
  again.
- Without `secret_cache=`, `secrets=` uses a **process-wide shared** cache
  per (region, session), with TTL 300, created the first time `secrets=` is
  used. There are at most 32 (`MAX_SHARED_CACHES`): a process that creates
  a session or a credentials provider per tenant or per request
  discards the least recently used one (it only costs a re-read later).
- **Expired values are discarded**, they do not stay in memory: each use
  of a cache (and each time a shared one is requested, for all the
  shared ones) first deletes the values whose TTL has passed; `sweep()` does it
  by hand. There is no timer: a cache nobody uses again keeps
  its values until the process drops it.
- Values live **only in the SDK process's memory**; `repr`,
  `str`, `toJSON` and `inspect` show `***`.

## Versions and metadata { #versiones-y-metadatos }

- E2B's integer version is encoded in Secrets Manager's `ClientRequestToken`
  (`rayito-secret-version-{n:020d}`, 42 characters): `create` is 1
  and each `update` writes `n + 1`, where `n` is the highest Rayito version
  that `DescribeSecret` still lists (not just the `AWSCURRENT` one: after an
  external rotation the current one is not Rayito's). Two writers with different
  values that compute the same `n + 1` collide: the second gets
  `SecretException` and must retry. If several consecutive external rotations
  leave Rayito's versions without a label, `DescribeSecret` no longer
  lists them and `update` may repeat a used number: create a new
  secret in that case.
- `update` more than once every 600 s for the same secret warns once
  (Secrets Manager recommends not writing more than once every 10 minutes and
  keeps at most 100 versions plus those of the last 24 h).
- `metadata` goes in `Description` as `rayito:v1:` + compact JSON, ≤ 2048
  characters (validated before calling AWS).
- `destroy` is `DescribeSecret` and, if the secret exists,
  `DeleteSecret(ForceDeleteWithoutRecovery=True)`: there is no recovery
  window. It returns `True` if it deleted it and `False` if it did not exist (or was already
  scheduled for deletion, or another process deleted it at the same time), without
  calling `DeleteSecret`, like E2B. The prior `DescribeSecret` is
  necessary because AWS accepts a forced deletion of a name that does not exist
  without `ResourceNotFoundException`.
- **Recreating a just-deleted name**: deletion is asynchronous and AWS takes
  a while to release the name (in the 0.5.0 acceptance, between 19 and 28 s).
  Meanwhile `CreateSecret` answers `InvalidRequestException`
  mentioning the deletion and `create` retries with the same
  `ClientRequestToken`, with exponential backoff (0.5 s doubling up to 8 s,
  ±25 % jitter) for at most **60 s**
  (`CREATE_RETRY_BUDGET_SECONDS` in Python, `CREATE_RETRY_BUDGET_MS` in
  TypeScript). After that deadline it raises `SecretException`
  (`SecretError`) with `aws_code="InvalidRequestException"`.
- **`list()` is eventually consistent**: `ListSecrets` can take
  ~3–5 s to show a just-created secret or the change of a just-updated one
  (and to stop showing a just-deleted one). `get_info`,
  `exists` and `destroy` (`DescribeSecret`) do not have that delay. A test
  that creates and lists must **poll** `list()` with a deadline, not check it
  just once.

## Errors { #errores }

| Python | TypeScript | When |
|---|---|---|
| `SecretNotFoundException` (also `NotFoundException`) | `SecretNotFoundError` | the secret does not exist or is scheduled for deletion |
| `SecretException` (a `SandboxException`) | `SecretError` (a `SandboxError`) | missing IAM permission (names the action), already exists, version collision, version limit, binary secret |
| `RateLimitException` | `RateLimitError` | `ThrottlingException` after the AWS SDK's retries |
| `InvalidArgumentException` | `InvalidArgumentError` | invalid name, value (≤ 64 KiB), `metadata`, TTL or variable; `envs`/`secrets` conflict; a webhook signing secret (`rayito/webhooks/...`) in `secrets=` or `SecretCache` |

No error contains the secret's value or name; AWS's message
is not propagated either (it may name the secret): only its code. Neither do
Rayito's logs; the AWS SDK's DEBUG log does contain them (see the warning in
[What it turns on and what it costs](#que-activa-y-que-cuesta)).

## How to turn it off { #como-apagarlo }

1. Remove `secrets=` / `secret_cache=` (TS: `secrets` / `secretCache`) from
   your calls and stop instantiating `SecretStore`: Rayito goes back to the
   0.4.0 path.
2. **Delete the secrets** you no longer use (`SecretStore().destroy(name)`,
   `Secret.destroy(name)` or the console): Secrets Manager bills for them
   ($0.40/month each) **until they are deleted**, whether or not you use Rayito.
3. If you deployed the policy stack, delete it:
   `rayito stack destroy secrets-access`.

Threat model: T18 of
[`SECURITY.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/SECURITY.md)
and [Security](security.md#custodia-de-secretos-del-usuario-t18).
