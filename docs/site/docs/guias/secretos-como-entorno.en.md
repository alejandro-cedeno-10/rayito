---
title: Secrets as environment variables
description: How to give your agent AWS Secrets Manager values as environment variables (a database URL, the key of an LLM gateway) with secret_env, secret_env_from, --secret-env and --secret-env-from, on Lambda MicroVMs and on AgentCore, with least privilege and without the value ever showing up in the plan, the image or the logs.
---

# Secrets as environment variables

Your agent already reads its configuration from environment variables
(`DATABASE_URL`, `LLM_GATEWAY_KEY`...), and the values live in AWS Secrets
Manager. `[env]` is not the way: it refuses anything that looks like a secret,
and AgentCore has no [secret gateway](../funciones-opcionales/pasarela-de-secretos.md).
`secret_env` and `secret_env_from` put those values in the agent's environment
at startup, without touching its code, and the project only holds **names**:
the variable's and the secret's, never the value (ADR-033 D4).

!!! info "Cost and activation"
    - **By default**: off. Without `[secret_env]`, `[[secret_env_from]]`,
      `--secret-env` or `--secret-env-from`, the SDK builds no Secrets Manager
      client and the plan, the image and the runtime environment are the usual
      ones.
    - **Turns it on**: `[secret_env]` or `[[secret_env_from]]` in
      `rayito-agent.toml`, `--secret-env` or `--secret-env-from` on
      `rayito agent deploy`, or `secret_env=` / `secretEnv` on `AgentProject`.
      `--create-secrets-from` (or `create_secrets_from` / `createSecretsFrom`)
      also **creates** a secret.
    - **AWS resources and calls**: one `GetSecretValue` per secret when each
      session starts (AgentCore, with the runtime role) or each VM starts
      (MicroVMs, with your credentials). With `--create-secrets-from`, one
      `CreateSecret` (or `PutSecretValue` when it already exists) of the
      secret `rayito/agents/<name>/env`.
    - **Approximate cost** (us-east-1, checked 2026-09-30,
      [pricing](https://aws.amazon.com/secrets-manager/pricing/)): $0.05 per
      10,000 calls; the secret that `--create-secrets-from` creates, $0.40 per
      month until you delete it.
    - **IAM**: on AgentCore, Rayito gives the runtime role
      `secretsmanager:GetSecretValue` on those secrets only; on MicroVMs, the
      same permission on your credentials (`RayitoSecretsReader`). For
      `--create-secrets-from`, `secretsmanager:CreateSecret`,
      `secretsmanager:DescribeSecret` and `secretsmanager:PutSecretValue`.
    - **How to turn it off**: drop the tables or the flags; delete the created
      secret (`SecretStore(prefix="").destroy(...)` or the Secrets Manager
      console) when you no longer use it.

## Declaring them { #declararlos }

In the project file, each variable names a secret by its full name (without
the `rayito/` prefix of `SecretStore`, like `--readable-secret`) or by its ARN;
`key` takes one key of a JSON secret and `version_stage`, another version label
(by default, `AWSCURRENT`):

```toml
[secret_env]
DATABASE_URL = "app/db-url"
LLM_GATEWAY_KEY = { secret = "arn:aws:secretsmanager:us-east-1:123456789012:secret:llm-AbCdEf", key = "api_key" }

[[secret_env_from]]
secret = "app/config"
prefix = "APP_"
keys = ["DB_HOST", "DB_USER"]
```

`[[secret_env_from]]` takes **every key** of a JSON secret as a variable, with
`prefix` in front. With `keys` it only takes those (and they must exist);
without `keys`, every key of the secret must be a valid variable name: none is
dropped silently.

From the CLI, the same two pieces, repeatable, add to what the file has:

```bash
rayito agent deploy ./mi-agente --target agentcore \
  --secret-env DATABASE_URL=app/db-url \
  --secret-env 'LLM_GATEWAY_KEY=arn:aws:secretsmanager:us-east-1:123456789012:secret:llm-AbCdEf#api_key' \
  --secret-env-from 'app/config,prefix=APP_,keys=DB_HOST|DB_USER'
```

`#` separates the JSON key because Secrets Manager does not allow it in names
or ARNs. The rules are those of the file:

- the variable name must be an identifier and not a reserved name (`PATH`,
  `HOME`, `PYTHONPATH`, `NODE_OPTIONS`, anything starting with `AWS_`, `LD_` or
  `RAYITO_`, and those the runtime already sets);
- the secret must be a name or an ARN, never the pasted value
  (`secret_value`);
- a variable cannot be both in `[env]` and in `[secret_env]` (`conflict`);
- at most {{agentSecretEnvMaxSecrets}} secrets and
  {{agentSecretEnvMaxTotalBytes}} bytes across all values.

From the SDK, with the project you already have:

=== "Python"

    ```python
    from rayito import AgentDeployOptions, deploy_agent

    fleet = deploy_agent(
        "./mi-agente",
        target="agentcore",
        options=AgentDeployOptions(check_env=True),
    )
    print(fleet.invoke('{"ticket_id": "T-1"}').text)
    ```

=== "TypeScript"

    ```typescript
    import { deployAgent } from "rayito";

    const fleet = await deployAgent("./mi-agente", {
      target: "agentcore",
      options: { checkEnv: true },
    });
    console.log((await fleet.invoke('{"ticket_id": "T-1"}')).text);
    ```

`AgentProject(secret_env=..., secret_env_from=...)` (TypeScript `secretEnv`,
`secretEnvFrom`) takes strings, `SecretEnvRef` and `SecretEnvSource`.

## How they reach the agent { #como-llegan }

**On AgentCore** the runtime only receives the `RAYITO_SECRET_ENV` variable,
with the variable names and the secret names or ARNs (never a value). When each
session starts, before loading your code, the agent host takes it out of its
environment, reads each secret with the runtime role (a botocore tool in the
same venv the image already ships for credentials), sets the variables and only
logs `secret_env_loaded count=N`. As each session is a new microVM, a new
session sees the current value of the secret after a rotation; running ones
keep theirs.

The secrets join those of `--readable-secret`: the runtime role can read
**exactly** those and nothing else (`ReadableSecretArns`), so the stack
template does not change. The plan says so under «Seguridad» (`secretEnv`)
and, with `--json`, in `secretEnvNames`.

**On MicroVMs** the SDK reads the values with your credentials right before
starting the host and passes them as variables of its process: they go to no
file and to no image. It also covers fleets and warm slots. For a model key, a
[gateway](../funciones-opcionales/pasarela-de-secretos.md) is still better: the
agent uses the key without being able to read it.

If anything fails, the deployment fails closed and the agent does not start:
on AgentCore the host leaves the code in its failure marker; on MicroVMs, the
SDK raises `AgentException(reason="runtime_error")`. The `detail_code` says
what happened, never naming the secret or the value:

| `detail_code` | What happened |
|---|---|
| `secret_env_unavailable` | the secrets could not be read (network, credentials, the {{agentSecretEnvHelperTimeoutSeconds}} s deadline) |
| `secret_env_denied` | the role cannot read a secret, or it does not exist for it |
| `secret_env_invalid` | a value is not what the reference asks for (a JSON, a string key) or goes over the cap |
| `secret_env_invalid_key` | a requested key is missing, or a key is not a valid variable name |
| `secret_env_reserved` | a `secret_env_from` key would give a reserved name |

## Creating the secret from a `.env` { #crear-desde-env }

If the values are in a local `.env`, `--create-secrets-from` uploads them to a
single JSON secret, `rayito/agents/<name>/env` (or writes a new version if it
already exists), and wires it as a `secret_env_from` with those keys:

```bash
rayito agent deploy ./mi-agente --target agentcore --create-secrets-from ./mi-agente.env --yes
```

Before creating anything it shows the secret name and **only the names** of
the variables, and asks for confirmation (`--yes`, or an answer in a terminal):
the secret costs $0.40 per month. The `.env` reader is minimal: `NAME=value`,
`export`, quotes and comments, no interpolation. It is the only time Rayito
reads values from a `.env`, and only because you ask it to.

## Warning about what is missing { #check-env }

`--check-env` (`check_env=True` / `checkEnv: true`) compares the names in your
`.env.example` (or `.env.sample`, `.env.template`) with what the agent will
receive (`[env]`, `secret_env`, the listed keys of `secret_env_from`, the
gateway variables and the AWS ones) and warns about each one left without a
value (`env_missing`). It only reads names, never a value. With `secret_env`
the warning always shows.

## The security trade-off { #seguridad }

A value in the environment is readable by all the agent's code:

- the agent process and everything it starts can read its environment
  (`/proc/self/environ`, a debug dump);
- on AgentCore, also, any code in the session can ask the microVM metadata
  service for the role credentials and read those secrets;
- on MicroVMs, whoever holds the sandbox access token sees the process
  environment with `commands.list()`, and a pause keeps the values in the
  memory snapshot.

Give the agent only the secrets it needs. More in
[Security](../security.md#secretos-como-entorno).

## What is left out { #fuera }

- Secrets encrypted with your own KMS key: the role has no `kms:Decrypt` and
  startup fails with `secret_env_denied`. Use the AWS managed key
  (`aws/secretsmanager`).
- Secrets of another account: an ARN of another account is
  `iam_resource_invalid`.
- `export(target="docker")` does not include them (the context README says so):
  set them with the platform where you run the container.
- An older SDK does not know `[secret_env]` and refuses the file
  (`unknown_key`).

## See also { #ver-tambien }

- [Deploy to AgentCore](desplegar-en-agentcore.md#secretos-como-entorno)
- [Deploy your agent](despliega-tu-agente.md)
- [Secrets](../secrets.md)
- [Errors](../referencia/errores.md)
- [Optional features and their cost](../optional-features.md)
