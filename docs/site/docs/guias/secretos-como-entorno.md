---
title: Secretos como variables de entorno
description: Cómo darle a tu agente valores de AWS Secrets Manager como variables de entorno (una URL de base de datos, la clave de una pasarela de LLM) con secret_env, secret_env_from, --secret-env y --secret-env-from, en Lambda MicroVMs y en AgentCore, con el mínimo privilegio y sin que el valor salga nunca en el plan, la imagen ni los logs.
---

# Secretos como variables de entorno

Tu agente ya lee su configuración de variables de entorno (`DATABASE_URL`,
`LLM_GATEWAY_KEY`...), y los valores viven en AWS Secrets Manager. `[env]` no
sirve para eso: rechaza lo que parece un secreto, y en AgentCore no hay
[pasarela de secretos](../funciones-opcionales/pasarela-de-secretos.md).
`secret_env` y `secret_env_from` ponen esos valores en el entorno del agente al
arrancar, sin tocar su código, y el proyecto sólo lleva **nombres**: el de la
variable y el del secreto, nunca el valor (ADR-033 D4).

!!! info "Coste y activación"
    - **Por defecto**: apagado. Sin `[secret_env]`, `[[secret_env_from]]`,
      `--secret-env` ni `--secret-env-from`, el SDK no construye ningún cliente
      de Secrets Manager y el plan, la imagen y el entorno del runtime son los
      de siempre.
    - **Activa**: `[secret_env]` o `[[secret_env_from]]` en
      `rayito-agent.toml`, `--secret-env` o `--secret-env-from` en
      `rayito agent deploy`, o `secret_env=` / `secretEnv` en `AgentProject`.
      `--create-secrets-from` (o `create_secrets_from` / `createSecretsFrom`)
      además **crea** un secreto.
    - **Recursos y llamadas AWS**: un `GetSecretValue` por secreto al arrancar
      cada sesión (AgentCore, con el rol del runtime) o cada VM (MicroVMs, con
      tus credenciales). Con `--create-secrets-from`, un `CreateSecret` (o un
      `PutSecretValue` si ya existe) del secreto `rayito/agents/<nombre>/env`.
    - **Coste aproximado** (us-east-1, consultado 2026-09-30,
      [precios](https://aws.amazon.com/secrets-manager/pricing/)): $0,05 por
      cada 10 000 llamadas; el secreto que crea `--create-secrets-from`, $0,40
      al mes hasta que lo borres.
    - **IAM**: en AgentCore, Rayito da al rol del runtime
      `secretsmanager:GetSecretValue` sólo sobre esos secretos; en MicroVMs, el
      mismo permiso en tus credenciales (`RayitoSecretsReader`). Para
      `--create-secrets-from`, `secretsmanager:CreateSecret`,
      `secretsmanager:DescribeSecret` y `secretsmanager:PutSecretValue`.
    - **Cómo apagarla**: quita las tablas del fichero o las banderas; borra el
      secreto creado (`SecretStore(prefix="").destroy(...)` o la consola de
      Secrets Manager) si ya no lo usas.

## Declararlos { #declararlos }

En el fichero de proyecto, cada variable nombra un secreto por su nombre
completo (sin el prefijo `rayito/` de `SecretStore`, como `--readable-secret`)
o por su ARN; `key` toma una clave de un secreto JSON y `version_stage`, otra
etiqueta de versión (por defecto, `AWSCURRENT`):

```toml
[secret_env]
DATABASE_URL = "app/db-url"
LLM_GATEWAY_KEY = { secret = "arn:aws:secretsmanager:us-east-1:123456789012:secret:llm-AbCdEf", key = "api_key" }

[[secret_env_from]]
secret = "app/config"
prefix = "APP_"
keys = ["DB_HOST", "DB_USER"]
```

`[[secret_env_from]]` toma **cada clave** de un secreto JSON como una variable,
con `prefix` delante. Con `keys` sólo toma ésas (y deben existir); sin `keys`,
cada clave del secreto debe ser un nombre de variable válido: ninguna se
descarta en silencio.

Desde la CLI, las mismas dos piezas, repetibles, se suman a lo del fichero:

```bash
rayito agent deploy ./mi-agente --target agentcore \
  --secret-env DATABASE_URL=app/db-url \
  --secret-env 'LLM_GATEWAY_KEY=arn:aws:secretsmanager:us-east-1:123456789012:secret:llm-AbCdEf#api_key' \
  --secret-env-from 'app/config,prefix=APP_,keys=DB_HOST|DB_USER'
```

`#` separa la clave JSON porque Secrets Manager no lo admite en nombres ni en
ARNs. Las reglas son las mismas que en el fichero:

- el nombre de la variable debe ser un identificador y no un nombre
  reservado (`PATH`, `HOME`, `PYTHONPATH`, `NODE_OPTIONS`, cualquiera que
  empiece por `AWS_`, `LD_` o `RAYITO_`, y las que el runtime ya fija);
- el secreto debe ser un nombre o un ARN, nunca el valor pegado (`secret_value`);
- una variable no puede estar a la vez en `[env]` y en `[secret_env]`
  (`conflict`);
- como mucho {{agentSecretEnvMaxSecrets}} secretos y
  {{agentSecretEnvMaxTotalBytes}} bytes entre todos los valores.

Desde el SDK, con el proyecto que ya tienes:

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
`secretEnvFrom`) acepta cadenas, `SecretEnvRef` y `SecretEnvSource`.

## Cómo llegan al agente { #como-llegan }

**En AgentCore** el runtime sólo recibe la variable `RAYITO_SECRET_ENV`, con los
nombres de variable y los nombres o ARNs de los secretos (nunca un valor). Al
arrancar cada sesión, antes de cargar tu código, el host del agente la saca de
su entorno, pide cada secreto con el rol del runtime (una herramienta con
botocore en el mismo venv que la imagen ya trae para las credenciales), pone
las variables y sólo deja en el log `secret_env_loaded count=N`. Como cada
sesión es una microVM nueva, una sesión nueva ve el valor actual del secreto
tras una rotación; las que están en curso conservan el suyo.

Los secretos se suman a los de `--readable-secret`: el rol del runtime puede
leer **exactamente** esos y nada más (`ReadableSecretArns`), así que la
plantilla de la pila no cambia. El plan lo dice en «Seguridad» (`secretEnv`) y,
con `--json`, en `secretEnvNames`.

**En MicroVMs** el SDK lee los valores con tus credenciales justo antes de
arrancar el host y se los pasa como variables de su proceso: no van a ningún
fichero ni a la imagen. Vale también para las flotas y las plazas calientes.
Para la clave de un modelo, sigue siendo mejor una
[pasarela](../funciones-opcionales/pasarela-de-secretos.md): el agente la usa
sin poder leerla.

Si algo falla, el despliegue falla en cerrado y el agente no arranca: en
AgentCore el host deja el código en su marcador de fallo; en MicroVMs, el SDK
lanza `AgentException(reason="runtime_error")`. El `detail_code` dice qué
pasó, sin nombrar nunca el secreto ni el valor:

| `detail_code` | Qué pasó |
|---|---|
| `secret_env_unavailable` | no se pudieron leer los secretos (red, credenciales, plazo de {{agentSecretEnvHelperTimeoutSeconds}} s) |
| `secret_env_denied` | el rol no puede leer un secreto, o no existe para él |
| `secret_env_invalid` | un valor no es lo que la referencia pide (un JSON, una clave de tipo cadena) o se pasa del tope |
| `secret_env_invalid_key` | falta una clave pedida, o una clave no es un nombre de variable válido |
| `secret_env_reserved` | una clave de `secret_env_from` daría un nombre reservado |

## Crear el secreto desde un `.env` { #crear-desde-env }

Si los valores están en un `.env` local, `--create-secrets-from` los sube a un
único secreto JSON, `rayito/agents/<nombre>/env` (o escribe una versión nueva si
ya existe), y lo conecta como `secret_env_from` con esas claves:

```bash
rayito agent deploy ./mi-agente --target agentcore --create-secrets-from ./mi-agente.env --yes
```

Antes de crear nada enseña el nombre del secreto y **sólo los nombres** de las
variables, y pide confirmación (`--yes`, o una respuesta en una terminal): el
secreto cuesta $0,40 al mes. El lector de `.env` es mínimo: `NOMBRE=valor`,
`export`, comillas y comentarios, sin interpolación. Es la única vez que Rayito
lee valores de un `.env`, y sólo porque se lo pides.

## Avisar de lo que falta { #check-env }

`--check-env` (`check_env=True` / `checkEnv: true`) compara los nombres de tu
`.env.example` (o `.env.sample`, `.env.template`) con lo que va a recibir el
agente (`[env]`, `secret_env`, las claves listadas de `secret_env_from`, las
variables de las pasarelas y las de AWS) y avisa de cada una que se queda sin
valor (`env_missing`). Sólo lee nombres, nunca un valor. Con `secret_env` el
aviso sale siempre.

## El compromiso de seguridad { #seguridad }

Un valor en el entorno es legible por todo el código del agente:

- el proceso del agente y todo lo que lance pueden leer su entorno
  (`/proc/self/environ`, un volcado de depuración);
- en AgentCore, además, cualquier código de la sesión puede pedir las
  credenciales del rol al servicio de metadatos de la microVM y leer esos
  secretos;
- en MicroVMs, quien tenga el token de acceso del sandbox ve el entorno del
  proceso con `commands.list()`, y una pausa guarda los valores en la
  instantánea de memoria.

Da al agente sólo los secretos que necesita. Más en
[Seguridad](../security.md#secretos-como-entorno).

## Lo que queda fuera { #fuera }

- Secretos cifrados con una clave de KMS propia: el rol no tiene
  `kms:Decrypt` y el arranque falla con `secret_env_denied`. Usa la clave
  gestionada por AWS (`aws/secretsmanager`).
- Secretos de otra cuenta: un ARN de otra cuenta es `iam_resource_invalid`.
- `export(target="docker")` no los incluye (lo dice el README del contexto):
  ponlos con la plataforma donde corras el contenedor.
- Un SDK anterior no conoce `[secret_env]` y rechaza el fichero
  (`unknown_key`).

## Ver también { #ver-tambien }

- [Desplegar en AgentCore](desplegar-en-agentcore.md#secretos-como-entorno)
- [Despliega tu agente](despliega-tu-agente.md)
- [Secretos](../secrets.md)
- [Errores](../referencia/errores.md)
- [Funciones opcionales y su coste](../optional-features.md)
