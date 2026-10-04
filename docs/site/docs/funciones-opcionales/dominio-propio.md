# Dominio propio

## Qué hace

Sin esta función, un puerto del sandbox sólo es alcanzable mandando las
cabeceras `x-aws-proxy-auth`/`x-aws-proxy-port` que devuelve `get_host()`:
un navegador, un webhook o un `<iframe>` no pueden. Con dominio propio, cada
puerto tiene una URL HTTPS normal bajo **tu** dominio:

```text
https://<puerto>-<sandbox_id>.<tu dominio>     p. ej. https://8000-3f2a….sbx.example.com
```

que se abre desde un navegador sin cabecera del proxy. Por debajo hay una
distribución CloudFront **en tu cuenta** con una CloudFront Function que, en
cada petición, busca la ruta del hostname en un `KeyValueStore`, comprueba el
token de tráfico y reenvía al sandbox poniendo ella misma las cabeceras del
proxy de AWS.

!!! warning "Experimental (0.6): `CustomDomain` sí, `Sandbox.create(domain=)` todavía no"
    `CustomDomain` (desplegar/borrar la pila y `register`/`unregister`/
    `refresh` de rutas) está implementada y probada. Lo que **no** existe
    todavía es que `Sandbox.create(domain=...)`/`get_host()` devuelvan esa
    URL solos: `domain=` sigue lanzando `UnimplementedError` (seguimiento
    no bloqueante, ADR-024 de `ARCHITECTURE.md`). Hoy registras la ruta tú
    mismo y `route.host` es exactamente el hostname de arriba (ver el
    ejemplo). La Function de enrutado ya se comprobó contra el runtime real
    de CloudFront (`TestFunction`, Q121 de `AWS_API_NOTES.md`); el recorrido
    completo por una distribución (DOM-2/3/5/7/8) queda para la aceptación
    contra AWS real, que el e2e del repositorio automatiza. El primer
    intento (Q140, Q141) arregló un `Comment` demasiado largo en la plantilla
    y se paró en una SCP de la organización que deniega crear distribuciones.

!!! info "Coste y activación"
    - **Por defecto**: apagado. Sin instanciar `CustomDomain` el SDK no
      construye ningún cliente `cloudformation` ni `cloudfront-keyvaluestore`.
    - **Activa**: `CustomDomain(public_domain="sbx.example.com").deploy(certificate_arn=...)`
      o `rayito domain deploy`.
    - **Recursos y llamadas AWS**: una distribución CloudFront, su
      CloudFront Function de enrutado y un KeyValueStore
      (`infra/custom-domain.yaml`). En uso,
      `register()`/`unregister()`/`refresh()` llaman a
      `DescribeKeyValueStore`/`PutKey`/`DeleteKey`.
    - **Coste aproximado** (us-east-1,
      [precios de CloudFront](https://aws.amazon.com/cloudfront/pricing/);
      cifras de lista de CloudFront Functions/KeyValueStore desde su
      lanzamiento, por reconfirmar en la aceptación AWS): $0 en reposo;
      ~$0,085/GB + $0,0075/10 000 peticiones HTTPS de salida; la CloudFront
      Function, ~$0,10 por 1 000 000 de invocaciones (una por petición); el
      KeyValueStore, ~$0,50 por 1 000 000 de lecturas (las de la Function) y
      ~$5 por 1 000 000 de llamadas de gestión (`PutKey`/`DeleteKey`).
    - **IAM** (credenciales de quien llama al SDK): `cloudformation:*Stack*`
      para `deploy`/`status`/`destroy`, y los permisos de CloudFront para
      crear y borrar la distribución, la Function y el KeyValueStore (la
      pila corre con esas mismas credenciales);
      `cloudfront-keyvaluestore:DescribeKeyValueStore/PutKey/DeleteKey`
      sobre el KVS de la pila.
    - **Cómo apagarla**: `destroy()` o `rayito domain destroy` borra la
      distribución (tarda ~15 min en deshabilitarse primero), la Function y
      el KeyValueStore — ninguna ruta sobrevive, son efímeras. Borra también
      el `CNAME` que creaste en tu DNS.

## Cuándo usarlo

- Necesitas que un puerto del sandbox responda en una URL HTTPS normal, sin
  que el cliente tenga que añadir cabeceras: un navegador, un webhook, un
  `<iframe>`.
- **Cuándo no**: un cliente programático que ya sabe mandar
  `x-aws-proxy-auth`/`x-aws-proxy-port` — `get_host()` ya lo resuelve sin
  desplegar nada; para desarrollo local, `rayito sandbox proxy`.

## Activarlo

Necesitas tres cosas:

1. **Un dominio** bajo el que vivirán las URLs, p. ej. `sbx.example.com`.
2. **Un certificado ACM en `us-east-1`** (requisito de CloudFront, sin
   importar en qué región corran tus sandboxes) que cubra
   `*.sbx.example.com`.
3. **Un registro DNS**: tras desplegar, un `CNAME` (o alias, en Route 53)
   de `*.sbx.example.com` al `DistributionDomainName` que imprime el
   despliegue (`dxxxxxxxxxxxx.cloudfront.net`).

=== "CLI"

    ```bash
    rayito domain deploy --public-domain sbx.example.com \
      --certificate-arn arn:aws:acm:us-east-1:<cuenta>:certificate/<id>
    # ... DNS: apunta un CNAME/alias de *.sbx.example.com a dxxxxxxxxxxxx.cloudfront.net
    rayito domain status
    ```

=== "Python"

    ```python
    from rayito import CustomDomain

    domain = CustomDomain(public_domain="sbx.example.com")  # no llama a AWS
    status = domain.deploy(certificate_arn="arn:aws:acm:us-east-1:<cuenta>:certificate/<id>")
    print(status.outputs["DistributionDomainName"])  # destino del CNAME
    ```

=== "TypeScript"

    ```typescript
    import { CustomDomain } from "rayito";

    const domain = new CustomDomain({ publicDomain: "sbx.example.com" }); // no llama a AWS
    const status = await domain.deploy({
      certificateArn: "arn:aws:acm:us-east-1:<cuenta>:certificate/<id>",
    });
    console.log(status.outputs.DistributionDomainName); // destino del CNAME
    ```

Sin el SDK, la misma plantilla con CloudFormation:

```bash
curl -fsSLO https://raw.githubusercontent.com/alejandro-cedeno-10/rayito/main/infra/custom-domain.yaml
aws cloudformation deploy --stack-name rayito-custom-domain \
  --template-file custom-domain.yaml \
  --parameter-overrides PublicDomain=sbx.example.com CertificateArn=arn:aws:acm:us-east-1:<cuenta>:certificate/<id>
```

## Exponer un puerto

=== "Python"

    ```python
    import secrets

    from rayito import CustomDomain, Sandbox

    domain = CustomDomain(public_domain="sbx.example.com")
    domain.status()  # lee el KvsArn de la pila ya desplegada

    with Sandbox.create("rayito-base", allowed_ports=[8000]) as sbx:
        jwe = sbx.get_host(8000).headers["x-aws-proxy-auth"]  # (1)!
        traffic_token = secrets.token_urlsafe(32)  # (2)!
        route = domain.register(
            sbx.sandbox_id, 8000, endpoint=sbx.endpoint, jwe=jwe,
            traffic_token=traffic_token, ttl_seconds=2400,
        )
        print(f"https://{route.host}")  # (3)!
        # ... antes de que caduque, con un JWE recién acuñado (4):
        fresh_jwe = sbx.get_host(8000).headers["x-aws-proxy-auth"]
        domain.refresh(route, jwe=fresh_jwe, ttl_seconds=2400)
        domain.unregister(sbx.sandbox_id, 8000)
    ```

    1. El JWE que ya emite `get_host()`; es el mismo que valida el proxy de
       AWS Lambda MicroVMs. La Function lo añade por ti en cada petición.
    2. Quien abra la URL lo manda en la cabecera `e2b-traffic-access-token`
       o en la cookie `rayito_tt` (lo práctico en un navegador: tu
       aplicación la fija para `.sbx.example.com`). Sin token, `register()`
       rechaza la llamada — una ruta sólo es pública con `public=True`
       explícito, nunca por omisión.
    3. `https://8000-<sandbox_id>.sbx.example.com`.
    4. `refresh()` necesita un JWE nuevo: `get_host()` lo vuelve a acuñar
       bajo demanda. Volver a pasar el original sólo alarga el TTL de la
       ruta, no el token que comprueba el proxy de AWS.

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
    await domain.status(); // lee el KvsArn de la pila ya desplegada

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

## Quitarlo

=== "CLI"

    ```bash
    rayito domain destroy
    ```

=== "Python"

    ```python
    from rayito import CustomDomain

    CustomDomain(public_domain="sbx.example.com").destroy()  # espera hasta 30 min
    ```

=== "TypeScript"

    ```typescript
    import { CustomDomain } from "rayito";

    await new CustomDomain({ publicDomain: "sbx.example.com" }).destroy(); // espera hasta 30 min
    ```

Después borra el `CNAME` de tu DNS. El certificado ACM es tuyo y no se toca.

## Nombres explícitos en vez del comodín

Por defecto la distribución lleva el alias `*.<tu dominio>`. CloudFront no
admite el mismo alias en dos distribuciones, así que si otra distribución
ya tiene ese comodín, el despliegue falla. Puedes pasar una lista de
hostnames exactos (cada uno `<etiqueta>.<tu dominio>`, normalmente
`host_for(alias, puerto)`): ante un solape CloudFront elige el nombre más
específico, así que conviven con el comodín ajeno. Sólo las rutas cuyos
hostnames estén en la lista llegan a esta distribución; el coste no cambia.
Una excepción: si tu DNS ya tiene un registro comodín que apunta a la otra
distribución, CloudFront rechaza el nombre más específico hasta que crees
para él un registro propio que apunte a esta.

=== "CLI"

    ```bash
    rayito domain deploy --public-domain sbx.example.com \
      --certificate-arn arn:aws:acm:us-east-1:<cuenta>:certificate/<id> \
      --alternate-domain-name 8000-demo.sbx.example.com
    ```

=== "Python"

    ```python
    from rayito import CustomDomain

    domain = CustomDomain(public_domain="sbx.example.com")
    domain.deploy(
        certificate_arn="arn:aws:acm:us-east-1:<cuenta>:certificate/<id>",
        alternate_domain_names=[domain.host_for("demo", 8000)],  # 8000-demo.sbx.example.com
    )
    ```

=== "TypeScript"

    ```typescript
    import { CustomDomain } from "rayito";

    const domain = new CustomDomain({ publicDomain: "sbx.example.com" });
    await domain.deploy({
      certificateArn: "arn:aws:acm:us-east-1:<cuenta>:certificate/<id>",
      alternateDomainNames: [domain.hostFor("demo", 8000)], // 8000-demo.sbx.example.com
    });
    ```

## Probar sin tocar el DNS

Antes de crear el `CNAME` puedes comprobar la distribución conectando
directamente a su `*.cloudfront.net` y mandando tu hostname como SNI y como
`Host` (CloudFront elige la distribución por el `Host`):

```bash
curl --connect-to 8000-<sandbox_id>.sbx.example.com:443:dxxxxxxxxxxxx.cloudfront.net:443 \
  -H "e2b-traffic-access-token: <token>" https://8000-<sandbox_id>.sbx.example.com/
```

Así funciona también el e2e del repositorio
(`tests/e2e/test_m15_custom_domain.py`, `custom-domain.e2e.test.ts`): toma
el dominio y el certificado sólo de `RAYITO_E2E_DOMAIN` (un dominio cuyo
comodín cubre el certificado) y `RAYITO_E2E_CERT_ARN`, despliega una pila
propia por corrida (`rayito-cd-e2e-<id>`) con nombres explícitos aleatorios
para no chocar con ningún alias existente, no crea ningún registro DNS y lo
borra todo al terminar. Sin esas variables se salta.

## Cómo funciona

1. La distribución tiene un único origen "placeholder" en la plantilla —
   nunca se contacta de verdad.
2. Cada petición pasa primero por la CloudFront Function
   (`infra/functions/custom_domain_router.js`), que borra cualquier
   cabecera `x-aws-proxy-*` que el viewer haya mandado, lee la ruta del
   `KeyValueStore` por la primera etiqueta del hostname
   (`{puerto}-{alias}`), comprueba el token de tráfico si la ruta no es
   pública, y llama a `cf.updateRequestOrigin()` con el `endpoint` real del
   sandbox y las cabeceras del proxy que hacen falta.
3. Una ruta sin entrada en el `KeyValueStore`, o cuyo `ttl_seconds` ya
   pasó, recibe 404 directo de la Function: nunca llega al origen
   placeholder, y desde fuera no se distingue de una ruta que nunca
   existió.

## Limitaciones conocidas

- Sin `Sandbox.create(domain=)`/`expose()`/`get_host()` cableados, hay que
  registrar la ruta a mano (como en el ejemplo).
- El token de tráfico sólo se guarda como su hash sha256: si lo pierdes,
  genera uno nuevo y vuelve a registrar la ruta.
- Una ruta no se borra sola cuando el sandbox muere: llama a
  `unregister()` tú mismo. Sin eso, la ruta se acota sola por dos lados
  independientes: `ttl_seconds` de `register()`/`refresh()` (que la propia
  Function comprueba — 404) y la caducidad del JWE en sí, del lado del
  proxy de AWS (401/403), lo primero que llegue.
- No hay refresher automático (DOM-14): llama a `refresh()` antes de que
  caduque el JWE si la ruta debe vivir más.
- Los puertos `8080` y `9000` (puerto de hooks y reservado) no se pueden
  exponer, igual que en el resto del SDK.
- Si tu cuenta está en una organización de AWS, una SCP puede denegar
  `cloudfront:CreateDistribution` aunque tu rol sea administrador. Entonces
  `deploy()` falla con `StackException` (la pila termina en
  `ROLLBACK_COMPLETE`; el motivo, `AccessDenied ... explicit deny in a
  service control policy`, sale en los eventos de la pila,
  `aws cloudformation describe-stack-events`). No queda nada creado: el
  rollback borra la Function y el KeyValueStore, y `destroy()` quita la pila
  fallida. Pide al administrador de la organización que permita esa acción
  (Q141 de `AWS_API_NOTES.md`).
