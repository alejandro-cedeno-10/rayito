# Pasarela de secretos

Un agente que llama a una API externa (Anthropic, OpenAI, tu propio backend)
normalmente necesita que su código *tenga* la credencial: `secrets=` la
entrega como variable de entorno, legible por cualquier cosa que el agente
ejecute. `gateways=`/`gateways` ofrece la alternativa: `rayd` abre, dentro
del propio sandbox, un listener HTTP en loopback por cada ruta que declares.
El sandbox le manda peticiones sin credencial; la pasarela comprueba una
allowlist de método/ruta y un límite de peticiones por minuto, inyecta la
cabecera real (resuelta de Secrets Manager, nunca guardada en el sandbox) y
reenvía al `upstream` fijo que declaraste. El código del sandbox puede
**usar** el secreto; no puede **leerlo**.

<small>Desde 0.6.0 (M15, ADR-023).</small>

!!! info "Coste y activación"
    - **Por defecto**: apagado. Sin `gateways=`/`gateways`, `rayd` no abre
      ningún socket de loopback para esta función y el SDK no construye
      ningún cliente `secretsmanager` nuevo ni manda ningún
      `ConfigureSandbox`. Ninguna variable de entorno lo enciende
      (ADR-014 regla 4).
    - **Activa**: `Sandbox.create(gateways={"nombre": SecretGateway(...)})`
      (Python) / `Sandbox.create({ gateways: { nombre: new SecretGateway({...}) } })`
      (TypeScript).
    - **Recursos y llamadas AWS**: `secretsmanager:GetSecretValue` una vez
      por cabecera y TTL de la `SecretCache` que ya usa `secrets=`/`secrets`
      (un acierto no llama a AWS); ningún recurso nuevo — reutiliza
      `infra/secrets-access.yaml`.
    - **Coste aproximado** (us-east-1, consultado 2026-09-30,
      [precios](https://aws.amazon.com/secrets-manager/pricing/)): el de
      `SecretCache` ($0,05 por 10 000 llamadas) más el del secreto en sí
      ($0,40/mes) si no existía ya.
    - **IAM** (credenciales de quien llama al SDK, no el execution role: la
      pasarela corre dentro de `rayd`, nunca necesita el rol de ejecución):
      `RayitoSecretsReader` de `infra/secrets-access.yaml`.
    - **Cómo apagarla**: no pases `gateways=`/`gateways` (su valor por
      defecto).

## Qué hace, exactamente

Por cada ruta de `gateways=`/`gateways`:

1. `rayd` abre un `TcpListener` en `127.0.0.1:<puerto elegido por el SO>`.
2. Una petición entrante se compara contra la allowlist `allow`
   (`(método, ruta)`, exacta o con sufijo `/*`) y el límite
   `rate_per_minute`/`ratePerMinute` (un cubo de tokens; `0` usa el valor
   por defecto, 600). Si no pasa ninguna de las dos comprobaciones, la
   pasarela responde sin abrir ninguna conexión al `upstream` (403 fuera de
   la allowlist, 429 por encima del límite).
3. Si pasa, `rayd` elimina de la petición cualquier cabecera cuyo nombre
   coincida con una de `headers` (así el sandbox nunca puede suplantar ni
   leer de vuelta su propia credencial) e inyecta el valor real de cada
   una.
4. Reenvía al `upstream` (siempre `https://host`, sin ruta ni query: esos
   vienen de cada petición) por un cliente HTTPS compartido que nunca
   resuelve un host a loopback, link-local o la IMDS del propio guest. La
   petición y la respuesta se transmiten en flujo: una respuesta en
   Server-Sent Events o una subida troceada atraviesan la pasarela sin
   cambios.

El valor de cada cabecera vive sólo en la memoria de `rayd`, nunca en
`repr`/`Debug`, nunca en un log: se resuelve justo antes de cada
`ConfigureSandbox` (el propio RPC nunca se registra) y se expone una única
vez, al construir la petición saliente.

## Límites

| | Valor | Por qué |
|---|---|---|
| Rutas por sandbox | 8 | cada una abre su propio listener; un límite evita agotar el rango de puertos efímeros del guest |
| Cabeceras por ruta | 16 | — |
| Reglas de `allow` por ruta | 32 | — |
| `rate_per_minute`/`ratePerMinute` | `0` (= 600) o 1..6000 | 600 = 10 peticiones/s, un tope conservador para un agente llamando a una sola API de forma interactiva |
| Nombre de ruta | 1-64 `[a-z0-9-]` | es la clave pública de `sbx.gateways["nombre"]` |

## Ejemplos

=== "Python"

    ```python
    from rayito import Sandbox, SecretCache, SecretGateway, SecretStore

    store = SecretStore(region="us-east-1")          # no llama a AWS
    store.create("anthropic", "sk-ant-...")

    sbx = Sandbox.create(
        gateways={
            "anthropic": SecretGateway(
                upstream="https://api.anthropic.com",
                headers={"x-api-key": "anthropic"},   # nombre del secreto, no el valor
                allow=[("POST", "/v1/messages")],
                rate_per_minute=600,
            )
        },
        secret_cache=SecretCache(),
    )
    url = sbx.gateways["anthropic"].url               # "http://127.0.0.1:<puerto>"
    sbx.commands.run("python agent.py", envs={"ANTHROPIC_BASE_URL": url})

    # rotar la credencial sin recrear el sandbox:
    store.update("anthropic", "sk-ant-nueva")
    sbx.gateways.refresh()

    sbx.kill()
    ```

=== "TypeScript"

    ```ts
    import { Sandbox, SecretCache, SecretGateway, SecretStore } from "rayito";

    const store = new SecretStore({ region: "us-east-1" });   // no llama a AWS
    await store.create("anthropic", "sk-ant-...");

    const sbx = await Sandbox.create({
      gateways: {
        anthropic: new SecretGateway({
          upstream: "https://api.anthropic.com",
          headers: { "x-api-key": "anthropic" },   // nombre del secreto, no el valor
          allow: [["POST", "/v1/messages"]],
          ratePerMinute: 600,
        }),
      },
      secretCache: new SecretCache(),
    });
    const url = sbx.gateways.get("anthropic")!.url;   // "http://127.0.0.1:<puerto>"
    await sbx.commands.run("python agent.py", { envs: { ANTHROPIC_BASE_URL: url } });

    // rotar la credencial sin recrear el sandbox:
    await store.update("anthropic", "sk-ant-nueva");
    await sbx.gateways.refresh();

    await sbx.kill();
    ```

## Divergencias con E2B

E2B no tiene un equivalente: no tiene conmutador de proxy interno al
sandbox que inyecte una credencial de un backend externo. `rayito.e2b` no
añade ningún método para esta función; `Sandbox.create(gateways=)` es
nativo de Rayito.

Contrato interno (sin AWS; `AWS_API_NOTES.md` §28 documenta la reutilización
de Secrets Manager, ya cubierta por §19).
