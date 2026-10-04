# docs-delta: m15-secrets-gateway

Filas y bloques exactos para que `m15-docs-integration` los aplique a los
ficheros compartidos. Esta función **no** toca ninguno de los ficheros de
abajo directamente.

## `docs/site/docs/e2b-parity.md`

Fila 14 (ya existe: "inyección de cabeceras / placeholders de IAM" de
`network.rules`): reemplazar por una nueva columna "Estado" — Rayito ahora
tiene un análogo, aunque con una forma distinta. **No tocar las filas
110-112** (dominio propio, s3-mounts, CLI: de otras features).

Reemplazo de la fila 14 completa:

```
| 14 | `network.rules` / `SandboxNetworkRule` (inyección de cabeceras, placeholders de IAM) | divergente (0.6.0) | Lambda MicroVMs no tiene gancho de egress en el host, así que no hay un inyector genérico por regla de host; en su lugar, `gateways=`/`gateways` abre dentro del propio guest un listener de loopback por ruta declarada, con una allowlist de método/ruta y un único `upstream` fijo (nunca una regla por host arbitrario) | [Pasarela de secretos](funciones-opcionales/pasarela-de-secretos.md) |
```

## `docs/site/docs/optional-features.md`

### "De un vistazo" (tabla tras `## De un vistazo`)

Nueva fila, al final de la tabla:

```
| [Pasarela de secretos](funciones-opcionales/pasarela-de-secretos.md) | apagada | `gateways=` / `gateways`: listener de loopback por ruta que inyecta una cabecera vaultada y reenvía a un `upstream` fijo, dentro de una allowlist y un límite de peticiones | el de `SecretCache` ($0,05/10 000 llamadas) + el del secreto si no existía | leer los secretos que vaultee (`RayitoSecretsReader`) | no pasar `gateways=` |
```

### Sección de ejemplo (tras `## Ejemplos`)

Nuevo bloque, al final de la sección (antes del siguiente `##` si lo hay):

````
<a id="secrets-gateway"></a>

### Pasarela de secretos

Guía completa: [Pasarela de secretos](funciones-opcionales/pasarela-de-secretos.md).

=== "Python"

    ```python
    from rayito import Sandbox, SecretCache, SecretGateway

    with Sandbox.create(
        gateways={"anthropic": SecretGateway(
            upstream="https://api.anthropic.com",
            headers={"x-api-key": "anthropic"},
            allow=[("POST", "/v1/messages")],
        )},
        secret_cache=SecretCache(),
    ) as sbx:
        url = sbx.gateways["anthropic"].url
        sbx.commands.run("python agent.py", envs={"ANTHROPIC_BASE_URL": url})
    ```

=== "TypeScript"

    ```ts
    import { Sandbox, SecretCache, SecretGateway } from "rayito";

    await using sbx = await Sandbox.create({
      gateways: { anthropic: new SecretGateway({
        upstream: "https://api.anthropic.com",
        headers: { "x-api-key": "anthropic" },
        allow: [["POST", "/v1/messages"]],
      }) },
      secretCache: new SecretCache(),
    });
    const url = sbx.gateways.get("anthropic")!.url;
    await sbx.commands.run("python agent.py", { envs: { ANTHROPIC_BASE_URL: url } });
    ```

**El código del sandbox no puede leer el secreto**: sólo puede usarlo, a
través del `upstream` fijo de la ruta.
````

### "Funciones con coste AWS" (tabla tras `## Funciones con coste AWS`)

Nueva fila, al final de la tabla:

```
| [Pasarela de secretos](#secrets-gateway) | disponible (0.6.0) | `gateways=` | `gateways` | `None` / `undefined` | Abre, dentro de `rayd`, un listener de loopback por ruta declarada que reenvía sólo lo que su `allow` cubre (dentro de su límite de peticiones por minuto), inyectando la cabecera vaultada y eliminando antes cualquier cabecera del mismo nombre que el sandbox intente poner | `secretsmanager:GetSecretValue` una vez por cabecera y TTL de `SecretCache` (un acierto no llama a AWS) y una por cabecera en cada `sbx.gateways.refresh()` (siempre relee); ningún recurso nuevo (reutiliza `infra/secrets-access.yaml`). También `pool.take(gateways=)` | SM: $0,05/10 000 llamadas + $0,40/secreto-mes si no existía ya ([precios de Secrets Manager](https://aws.amazon.com/secrets-manager/pricing/), consultado 2026-09-30, us-east-1) | `secretsmanager:GetSecretValue` en las credenciales del **llamante** (`RayitoSecretsReader` de `infra/secrets-access.yaml`); ninguno para el execution role | No pasar `gateways=` / `gateways` (o pasar `None`/`undefined`) | `clients/python/src/rayito/_secret_gateway/` / `clients/typescript/src/secret-gateway/` |
```

## `SECURITY.md`

Nueva fila en la tabla "Amenazas y mitigaciones", insertada después de T19
(o después de T23 si otra función de M15 ya la añadió):

```
| T24 | pasarela de secretos en loopback | M15 (`m15-secrets-gateway`, extiende T18): `gateways=` abre dentro de `rayd` un listener por ruta que inyecta una cabecera vaultada en peticiones hacia un `upstream` fijo. Riesgos: que el código del sandbox lea el valor, que suplante la cabecera para robarla de vuelta, que la pasarela reenvíe a un host no declarado (confuso-diputado/SSRF) o que no respete su propia allowlist/límite de tasa | **El valor nunca llega al código del sandbox**: vive sólo en la memoria de `rayd` (`SecretValue`, `Zeroizing`, sin `Debug`/`Display`/`serde`), se resuelve justo antes de cada `Configure` (con la misma `SecretCache` de T18) y se expone una única vez, al construir la cabecera saliente; `ConfigureGrpc` nunca registra la petición. **Anti-suplantación**: antes de inyectar, el listener elimina de la petición entrante cualquier cabecera cuyo nombre coincida con una de las vaultadas, así que el sandbox no puede ponerla él mismo ni leerla de vuelta en un eco. **Allowlist y límite antes de la red**: una petición fuera de `allow` (método+ruta exactos o prefijo `/*`) o por encima de `rate_per_minute`/`ratePerMinute` nunca abre una conexión al upstream (403/429, cubo de tokens entero y determinista). **Anti-confuso-diputado**: `upstream` es sólo `https://host` (sin ruta, query, usuario ni fragmento) y se alcanza por un cliente HTTPS compartido cuyo resolvedor rechaza loopback, link-local e IMDS (`FilteringResolver`); no hay redirección genérica a un host elegido por la petición. **Rutas sin atajos**: una ruta de petición con un segmento `.`/`..` (tal cual o codificado), un `/` o `\` codificado, una barra invertida o un segmento vacío se rechaza (403) antes de la allowlist; `rayd` nunca normaliza ni reenvía una ruta que el upstream pudiera normalizar fuera de `allow` (`/v1/../admin` no pasa por `/v1/*`). **Cabeceras que no rompen la petición**: los nombres de `headers` deben ser *tokens* RFC 9110, únicos ignorando mayúsculas y nunca `host`/`content-length`/`transfer-encoding` ni hop-by-hop (SDK y `rayd`). **Rotación y baja reales**: una ruta conserva puerto y listener al rotar, y la siguiente petición de cualquier conexión (keep-alive incluida) ya lleva el valor nuevo; una ruta retirada cierra sus conexiones keep-alive, así que ninguna conexión sigue reenviando con la credencial vieja. **Egress declarado**: el tráfico hacia el upstream sale como root (`rayd`, no uid 1000) — la única excepción de egress que abre esta función, reportada en `Health.features.root_egress` (`SecretGatewayUpstream`), nunca escondida. Apagado por defecto (ADR-014): sin `gateways=`/`gateways`, `rayd` no abre ningún socket de loopback para esta función y el SDK no construye ningún cliente nuevo | M15 |
```

Prosa (sección propia, tras la de T19/"Custodia de secretos del usuario"):

```
## Pasarela de secretos en loopback (T24)

`gateways=`/`gateways` (apagado por defecto) abre, dentro de `rayd`, un
listener de loopback por ruta declarada: el código del sandbox puede
**usar** la credencial que vaultea (en peticiones al `upstream` fijo de la
ruta, dentro de su allowlist y límite de tasa) pero no **leerla**. Detalle
en [Pasarela de secretos](site/docs/funciones-opcionales/pasarela-de-secretos.md)
y arriba, T24.
```

## `docs/site/docs/security.md`

Misma fila T24 que en `SECURITY.md` (tabla corta de esa página, formato de
una celda por mitigación resumida en vez de la mitigación completa) y la
misma sección `## Pasarela de secretos en loopback (T24)` con el detalle,
siguiendo el patrón de las secciones T15/T18/T19 ya existentes en esa
página (una celda corta en la tabla de arriba, remitiendo a "Detalle en
`SECURITY.md` T24").

## `docs/site/docs/referencia/errores.md`

### Árbol (Python)

Añadir bajo la rama de `SandboxException`, junto a las demás de M15:

```
├── GatewayException
```

### Árbol (TypeScript)

Añadir el equivalente bajo `SandboxError`:

```
├── GatewayError
```

### Tabla

Nueva fila, junto a las demás de Secrets Manager:

```
| `GatewayException` | `GatewayError` | el listener de loopback de `gateways=`/`gateways` | la petición no estaba en `allow`, excedió `rate_per_minute`/`ratePerMinute`, o el `upstream` no respondió (`code`: `not_allowed`, `rate_limited`, `upstream_unreachable`, `upstream_timeout`, `upstream_error`) | lee `code`; ver [Pasarela de secretos](../funciones-opcionales/pasarela-de-secretos.md) |
```

## `docs/site/docs/referencia/variables-de-entorno.md`

Sin cambios: `gateways=`/`gateways` no define ninguna variable de entorno
(ADR-014 regla 4).

## `docs/site/docs/cost.md`

Sin cambios: ni `m13-secrets` (la función más parecida, Secrets Manager)
añadió una fila aquí — el coste detallado vive en la guía de la función y
en `optional-features.md`, no en esta página de referencia de precios.

## `docs/site/docs/limits.md`

Sin cambios: `gateways=`/`gateways` no cambia el protocolo ni el agente
mínimo requerido más allá de lo que ya exige `ConfigureSandbox` en general
(fila "0.6" ya añadida por foundations).
