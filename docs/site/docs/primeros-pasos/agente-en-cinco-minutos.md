---
description: "Despliega un agente que ya tienes (LangGraph, Strands, deepagents, Vercel AI SDK…) en AWS con una orden y sin tocar su código: detecta, ve el coste, despliega, llámalo y páralo."
---

# Tu agente en 5 minutos

Tienes un agente que ya funciona en tu portátil y quieres tenerlo corriendo en
tu cuenta de AWS, aislado en un MicroVM y con su credencial del modelo fuera de
su alcance. Esta página lo hace con una orden y sin cambiar su código. Los
detalles de cada paso están en [Despliega tu agente](../guias/despliega-tu-agente.md).

## Qué necesitas

- [Rayito configurado en tu cuenta](configurar-aws.md), con la imagen
  `rayito-base-caps` publicada.
- Una clave de API de Bedrock en la variable `AWS_BEARER_TOKEN_BEDROCK`
  (o la de tu proveedor: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`…).
- Un proyecto de agente con su fichero de bloqueo (`uv.lock`, `requirements.txt`
  o `package-lock.json`) y el id del modelo en el código. Sirve un grafo de
  LangGraph, un agente de Strands, de deepagents, del Vercel AI SDK, de Hermes o
  una app HTTP.

## 1. Mira qué detecta

```bash
rayito agent detect ./mi-agente
```

![Resultado de rayito agent detect: lenguaje, framework, punto de entrada, protocolo, instalación y proveedor](../assets/agente/agente-detectado.png)

Lee los ficheros del proyecto, no ejecuta tu código ni llama a AWS. Si algo no
es lo que esperabas, lo cambias con `--framework`, `--entry` o escribiendo la
clave en un `rayito-agent.toml` (`--write` lo deja escrito para que lo edites).

## 2. Mira el plan y el coste

```bash
rayito agent deploy ./mi-agente --dry-run --max-sessions 2 --min-vms 1 --max-vms 2 --warm 1 --memory 4096
```

![Resultado de rayito agent deploy --dry-run: el plan, los VMs, el bucket, el secreto y el coste de lista](../assets/agente/agente-plan.png)

No crea nada. Te dice qué bucket, qué secreto y qué imagen base usaría, cuántos
VMs tendrías y cuánto cuesta cada uno por hora.

## 3. Despliégalo

```bash
export AWS_BEARER_TOKEN_BEDROCK=...        # tu clave; se guarda en Secrets Manager y no se escribe en disco
rayito agent deploy ./mi-agente --create-secret
```

La primera vez construye la imagen del agente (unos minutos: instala el runtime
y tus dependencias con sus versiones fijadas); las siguientes la reutilizan si no
cambió nada. Al terminar imprime el nombre de la flota y deja el access token en
`.rayito/agent-token` (modo `0600`; no lo subas al repositorio).

## 4. Llámalo

=== "CLI"

    ```bash
    rayito agent invoke mi-agente "¿Cuánto es 17 más 25?"
    ```

=== "Python"

    ```python
    from pathlib import Path

    from rayito import AgentFleet

    fleet = AgentFleet.connect(
        "mi-agente", access_token=Path(".rayito/agent-token").read_text().strip()
    )
    reply = fleet.invoke("¿Cuánto es 17 más 25?")
    print(reply.text)
    ```

=== "TypeScript"

    ```ts
    import { readFileSync } from "node:fs";
    import { AgentFleet } from "rayito";

    const fleet = await AgentFleet.connect("mi-agente", {
      accessToken: readFileSync(".rayito/agent-token", "utf8").trim(),
    });
    const reply = await fleet.invoke("¿Cuánto es 17 más 25?");
    console.log(reply.text);
    ```

## 5. Páralo

```bash
rayito agent stop mi-agente
```

Termina todos los VMs de la flota. Si no lo paras, vive como mucho 8 horas y
cobra mientras esté en marcha; con `--idle 300` se suspende solo cuando nadie
lo usa y no cobra cómputo.

## Siguiente

- [Varias sesiones y varias VMs](../guias/flota-de-agentes.md): más de una
  conversación a la vez, cola, plazas calientes y qué pasa si la app se cae.
- [Despliega tu agente](../guias/despliega-tu-agente.md): el fichero de proyecto,
  los frameworks y cómo llega la credencial al modelo.
- [Desplegar en AgentCore](../guias/desplegar-en-agentcore.md): el mismo
  proyecto en Amazon Bedrock AgentCore Runtime con `--target agentcore`, sin
  Docker local (la imagen se construye en CodeBuild).
- [Desplegar con CDK](../guias/desplegar-con-cdk.md): el agente como un
  constructo `RayitoAgent` en tu app de CDK, desplegado con `cdk deploy`.
