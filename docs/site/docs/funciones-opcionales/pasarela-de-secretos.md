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
**usar** el secreto; no puede **leerlo**, salvo que el `upstream` permitido
lo refleje en su respuesta (ver [Lo que la pasarela no puede
impedir](#lo-que-la-pasarela-no-puede-impedir)).

<small>Desde 0.6.0 (M15, ADR-023).</small>

!!! info "Coste y activación"
    - **Por defecto**: apagado. Sin `gateways=`/`gateways`, `rayd` no abre
      ningún socket de loopback para esta función y el SDK no construye
      ningún cliente `secretsmanager` nuevo ni manda ningún
      `ConfigureSandbox`. Ninguna variable de entorno lo enciende
      (ADR-014 regla 4).
    - **Activa**: `Sandbox.create(gateways={"nombre": SecretGateway(...)})`
      o `pool.take(gateways=...)` (Python) /
      `Sandbox.create({ gateways: { nombre: new SecretGateway({...}) } })` o
      `pool.take({ gateways })` (TypeScript). Exige una imagen 0.6.0 o
      posterior: con una anterior, `create()`/`take()` termina el MicroVM
      recién lanzado o tomado (salvo `keep_on_failure`/`keepOnFailure` en
      `create()`) y lanza `UnimplementedError`.
    - **Recursos y llamadas AWS**: `secretsmanager:GetSecretValue` una vez
      por cabecera y TTL de la `SecretCache` que ya usa `secrets=`/`secrets`
      (un acierto no llama a AWS), y una más por cabecera en cada
      `sbx.gateways.refresh()` (siempre relee); ningún recurso nuevo —
      reutiliza `infra/secrets-access.yaml`.
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
2. La ruta de la petición pasa una lista de permitidos antes de mirar
   la allowlist, o se rechaza con 403: sólo caracteres de ruta de
   RFC 3986 salvo `;` (letras, dígitos, `-._~`, `:@`, `!$&'()*+,=` y
   `%XX` bien formados), ningún segmento vacío (`//`) salvo el último, y
   cada segmento, decodificado una sola vez, debe ser UTF-8 válido, sin
   bytes de control, sin `/`, `\`, `%` ni `;`, y distinto de `.` y `..`.
   `rayd` nunca normaliza la ruta, así que nunca reenvía una que el
   `upstream` (o la CDN delante de él) pudiera llevar fuera de `allow`:
   ni `/v1/../admin` ni `/v1/%2e%2e/admin`, ni `/v1/..;/admin` (Tomcat,
   Spring o Jetty quitan el parámetro `;` y ven `..`), ni
   `/v1/%252e%252e/admin` (un salto que decodifica dos veces ve `..`)
   pasan por una regla `/v1/*`. `/v1/files/a%20b` sí pasa. Las rutas de
   `allow` siguen la misma regla: el SDK rechaza una que ninguna
   petición podría cumplir con `InvalidArgumentException`/
   `InvalidArgumentError` antes de cualquier llamada, y `rayd` con
   `invalid_allow_path`.
3. Una petición entrante se compara contra la allowlist `allow`
   (`(método, ruta)`, exacta o con sufijo `/*`) y el límite
   `rate_per_minute`/`ratePerMinute` (un cubo de tokens; `0` usa el valor
   por defecto, 600). Si no pasa ninguna de las dos comprobaciones, la
   pasarela responde sin abrir ninguna conexión al `upstream` (403 fuera de
   la allowlist, 429 por encima del límite).
4. Si pasa, `rayd` elimina de la petición cualquier cabecera cuyo nombre
   coincida con una de `headers` (así el sandbox nunca puede suplantar su
   propia credencial) e inyecta el valor real de cada una.
5. Reenvía al `upstream` (siempre `https://host`, sin ruta, query,
   usuario ni fragmento: la ruta y la query vienen de cada petición) por
   un cliente HTTPS compartido que nunca resuelve un host a loopback, link-local o la IMDS del propio guest. La
   petición y la respuesta se transmiten en flujo: una respuesta en
   Server-Sent Events o una subida troceada atraviesan la pasarela sin
   cambios. Si el `upstream` no acepta la conexión en 10 s o no envía la
   cabecera de la respuesta en 600 s, la pasarela responde 504
   (`upstream_timeout`); cualquier otro fallo tras conectar (TLS, conexión
   cortada, respuesta malformada) es 502 (`upstream_error`). El cuerpo de
   una respuesta en flujo no tiene tope.
6. La respuesta del `upstream` llega al sandbox con su estado y su cuerpo
   **sin cambios**. De sus cabeceras, `rayd` elimina las de transporte, las
   que llevan el nombre de una de `headers` y cualquiera cuyo valor
   contenga uno de los valores inyectados (de 8 caracteres o más).

## Lo que la pasarela no puede impedir

!!! warning "Un endpoint que refleja la petición entrega el secreto"
    La pasarela no inspecciona el cuerpo de la respuesta. Si una regla de
    `allow` apunta a un endpoint que devuelve las cabeceras de la petición
    (rutas de depuración o de eco del tipo `/headers` o `/anything`,
    páginas de error verbosas que repiten `Authorization` o `x-api-key`),
    el código del sandbox lee el secreto en ese cuerpo. La garantía es «el
    sandbox puede usar el secreto y no puede leerlo **salvo que el
    `upstream` permitido lo refleje**»: es un límite que la pasarela no
    puede imponer por ti. Lista en `allow` sólo las rutas que tu agente
    necesita y nunca una de depuración o de eco
    ([`SECURITY.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/SECURITY.md), T24).

Los nombres de `headers` deben ser un *token* HTTP válido (RFC 9110), no
pueden repetirse ignorando mayúsculas (`X-Api-Key` y `x-api-key` son el
mismo) ni ser una cabecera de transporte que la pasarela fija ella misma
(`host`, `content-length`, `transfer-encoding`, `connection`, `keep-alive`,
`te`, `trailer`, `upgrade`, `proxy-authorization`, ...): el SDK lo rechaza
al construir `SecretGateway`, y `rayd` vuelve a comprobarlo.

El valor de cada cabecera vive sólo en la memoria de `rayd`, nunca en
`repr`/`Debug`, nunca en un log: se resuelve justo antes de cada
`ConfigureSandbox` (el propio RPC nunca se registra) y se expone una única
vez, al construir la petición saliente.

## Rotación

`sbx.gateways.refresh()` (`await sbx.gateways.arefresh()` en
`AsyncSandbox`) descarta de la `SecretCache` los secretos de la pasarela,
los vuelve a leer de Secrets Manager (aunque su TTL no haya vencido) y manda
un `ConfigureSandbox` nuevo. Cada ruta **conserva su puerto**: un proceso
arrancado con `ANTHROPIC_BASE_URL=http://127.0.0.1:<puerto>` sigue
funcionando, y su siguiente petición, incluso por una conexión keep-alive
ya abierta, lleva el valor nuevo. Si `rayd` rechaza la configuración
(por ejemplo un valor rotado con un salto de línea), `refresh()` lanza la
misma excepción que `create()` y la pasarela sigue con la configuración
anterior.

Una ruta que deja de estar en la configuración se cierra de verdad: deja de
aceptar conexiones, cierra en el acto las keep-alive inactivas y deja
terminar como mucho la petición en curso.

## Límites

| | Valor | Por qué |
|---|---|---|
| Rutas por sandbox | 8 | cada una abre su propio listener; un límite evita agotar el rango de puertos efímeros del guest |
| Cabeceras por ruta | 16 | — |
| Reglas de `allow` por ruta | 32 | — |
| `rate_per_minute`/`ratePerMinute` | `0` (= 600) o 1..6000 | 600 = 10 peticiones/s, un tope conservador para un agente llamando a una sola API de forma interactiva |
| Nombre de ruta | 1-64 `[a-z0-9-]` | es la clave pública de `sbx.gateways["nombre"]` |
| Espera a la cabecera de la respuesta | 600 s | basta para una petición larga a un LLM; un `upstream` atascado acaba en 504 en vez de colgar el comando para siempre |
| Conexión al `upstream` | 10 s | — |

## Ejemplos

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

    # rotar la credencial sin recrear el sandbox (mismo puerto, valor nuevo):
    store.update("anthropic", "sk-ant-nueva")
    sbx.gateways.refresh()

    sbx.kill()

    # desde un pool: las plazas calientes nunca llevan la pasarela
    with SandboxPool(PoolConfig(size=2, template="rayito-base")) as pool:
        sbx = pool.take(gateways={"anthropic": SecretGateway(
            upstream="https://api.anthropic.com",
            headers={"x-api-key": "anthropic"},
            allow=[("POST", "/v1/messages")],
        )})
    ```

=== "TypeScript"

    ```ts
    import { Sandbox, SandboxPool, SecretCache, SecretGateway, SecretStore } from "rayito";

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

    // rotar la credencial sin recrear el sandbox (mismo puerto, valor nuevo):
    await store.update("anthropic", "sk-ant-nueva");
    await sbx.gateways.refresh();

    await sbx.kill();

    // desde un pool: las plazas calientes nunca llevan la pasarela
    await using pool = await new SandboxPool({ size: 2, template: "rayito-base" }).start();
    const pooled = await pool.take({
      gateways: {
        anthropic: new SecretGateway({
          upstream: "https://api.anthropic.com",
          headers: { "x-api-key": "anthropic" },
          allow: [["POST", "/v1/messages"]],
        }),
      },
    });
    ```

## Divergencias con E2B

E2B no tiene un equivalente: no tiene conmutador de proxy interno al
sandbox que inyecte una credencial de un backend externo. `rayito.e2b` no
añade ningún método para esta función; `Sandbox.create(gateways=)` es
nativo de Rayito.

Contrato interno (sin AWS; `AWS_API_NOTES.md` §28 documenta la reutilización
de Secrets Manager, ya cubierta por §19).
