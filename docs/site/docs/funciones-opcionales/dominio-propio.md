# Dominio propio

Una distribución CloudFront **en tu cuenta** con un alias comodín
(`*.tu-dominio.com`) y una CloudFront Function que enruta cada petición al
sandbox correcto leyendo un `KeyValueStore`, para exponer un puerto como un
hostname público normal — sin las cabeceras `x-aws-proxy-auth`/
`x-aws-proxy-port` que `get_host()` exige en 0.5.x.

!!! warning "Experimental (0.6): construida pero sin cablear a Sandbox todavía"
    `CustomDomain` (deploy/status/destroy de la pila, y
    `register`/`unregister`/`refresh` de rutas) está implementada y
    probada. Lo que **no** existe todavía es la integración con
    `Sandbox.create(domain=...)`/`get_host()`/`expose()`: ese kwarg sigue
    lanzando `UnimplementedError` (seguimiento no bloqueante, ver
    `ARCHITECTURE.md` ADR-024 en el repositorio). Hoy se usa `CustomDomain`
    directamente, pasándole el `endpoint` del sandbox y un JWE que tú
    mismo acuñas. Además, DOM-2 (HTTP/1.1 real), DOM-3 (WebSocket), DOM-5
    (latencia de propagación del KeyValueStore), DOM-7 (mantener la ruta
    viva más allá de la caducidad del JWE), DOM-8 (auto-resume por el
    dominio) y DOM-14 (el refresher Lambda opcional, tampoco construido
    todavía) siguen sin medirse contra una distribución real: hace falta
    un dominio y un certificado ACM que sólo el mantenedor puede aportar.

!!! info "Coste y activación"
    - **Por defecto**: apagado. Sin instanciar `CustomDomain` el SDK no
      construye ningún cliente `cloudformation` ni `cloudfront-keyvaluestore`.
    - **Activa**: `CustomDomain(public_domain="sbx.tu-dominio.com").deploy(certificate_arn=...)`.
    - **Recursos y llamadas AWS**: una distribución CloudFront, su
      CloudFront Function de enrutado y un KeyValueStore
      (`infra/custom-domain.yaml`, `rayito domain deploy`). En uso,
      `register()`/`unregister()`/`refresh()` llaman a
      `DescribeKeyValueStore`/`PutKey`/`DeleteKey`.
    - **Coste aproximado** (us-east-1,
      [precios de CloudFront](https://aws.amazon.com/cloudfront/pricing/);
      cifras de lista de CloudFront Functions/KeyValueStore desde su
      lanzamiento, por reconfirmar en la etapa de aceptación AWS):
      ~$0,085/GB + $0,0075/10 000 peticiones HTTPS de salida; la CloudFront
      Function, ~$0,10 por 1 000 000 de invocaciones (una por petición); el
      KeyValueStore no cobra en reposo, ~$0,50 por 1 000 000 de lecturas
      (las que hace la Function) y ~$5 por 1 000 000 de llamadas de gestión
      (`PutKey`/`DeleteKey` de `register`/`unregister`/`refresh`).
    - **IAM** (credenciales de quien llama al SDK): `cloudformation:*Stack*`
      para `deploy`/`status`/`destroy`;
      `cloudfront-keyvaluestore:DescribeKeyValueStore/PutKey/DeleteKey`
      sobre el KVS de la pila.
    - **Cómo apagarla**: no llames a `deploy()`; `destroy()` borra la
      distribución (tarda ~15 min en deshabilitarse primero), la Function y
      el KeyValueStore — ninguna ruta sobrevive, son efímeras.

## Cuándo usarlo

- Necesitas que un puerto del sandbox responda en una URL HTTPS normal, sin
  que el cliente tenga que añadir cabeceras: un navegador, un webhook, un
  `<iframe>`.
- **Cuándo no**: un cliente programático que ya sabe mandar
  `x-aws-proxy-auth`/`x-aws-proxy-port` — `get_host()` en 0.5.x ya resuelve
  eso sin desplegar nada.

## Desplegar la pila

```bash
curl -fsSLO https://raw.githubusercontent.com/alejandro-cedeno-10/rayito/main/infra/custom-domain.yaml
aws cloudformation deploy --stack-name rayito-custom-domain \
  --template-file custom-domain.yaml \
  --parameter-overrides PublicDomain=sbx.tu-dominio.com CertificateArn=arn:aws:acm:us-east-1:<cuenta>:certificate/<id>
```

(Si clonaste el repositorio, la plantilla ya está en
`infra/custom-domain.yaml`; `rayito domain deploy` hace lo mismo sin salir
del SDK.) El certificado ACM **debe** estar en `us-east-1` (requisito de
CloudFront, sin importar en qué región despliegues tus sandboxes), y debe
cubrir `*.sbx.tu-dominio.com`. Tras desplegar, apunta un `CNAME`/`ALIAS` de
`*.sbx.tu-dominio.com` al `DistributionDomainName` que imprime el deploy.

## Ejemplo rápido (uso directo, sin `Sandbox.create(domain=)`)

=== "Python"

    ```python
    import secrets

    from rayito import CustomDomain, Sandbox

    domain = CustomDomain(public_domain="sbx.tu-dominio.com")  # (1)!
    domain.deploy(certificate_arn="arn:aws:acm:us-east-1:<cuenta>:certificate/<id>")

    with Sandbox.create("rayito-base", allowed_ports=[8000]) as sbx:
        jwe = sbx.get_host(8000).headers["x-aws-proxy-auth"]  # (2)!
        traffic_token = secrets.token_urlsafe(32)  # (3)!
        route = domain.register(
            sbx.sandbox_id, 8000, endpoint=sbx.endpoint, jwe=jwe,
            traffic_token=traffic_token, ttl_seconds=2400,
        )
        print(route.host)  # (4)!
        # ... antes de que caduque, con un JWE recién acuñado (5):
        fresh_jwe = sbx.get_host(8000).headers["x-aws-proxy-auth"]
        domain.refresh(route, jwe=fresh_jwe, ttl_seconds=2400)
        domain.unregister(sbx.sandbox_id, 8000)
    ```

    1. Construirlo no llama a AWS.
    2. El JWE que ya emite `get_host()`; es el mismo que valida el proxy de
       AWS Lambda MicroVMs.
    3. Guárdalo: hay que mandarlo en la cabecera `e2b-traffic-access-token`
       (o la cookie `rayito_tt`) de cada petición al `route.host`. Sin él,
       `register()` rechaza la llamada — una ruta sólo es pública con
       `public=True` explícito, nunca por omisión.
    4. `"8000-<sandbox_id>.sbx.tu-dominio.com"`.
    5. `refresh()` necesita un JWE nuevo, no el mismo que `register()` ya
       usó: `get_host()` lo vuelve a acuñar bajo demanda (o reutiliza el que
       el `TokenRefresher` interno del transporte ya renovó pasados
       `TOKEN_REFRESH_AFTER_MINUTES`). Volver a pasar el `jwe` original sólo
       extiende `m.x` (el TTL de la ruta) sin renovar el token que de
       verdad comprueba el proxy: en cuanto ese JWE original caduque, la
       ruta empieza a fallar aunque acabes de "refrescarla".

=== "Python (async)"

    ```python
    import asyncio
    import secrets

    from rayito import AsyncCustomDomain, AsyncSandbox


    async def main() -> None:
        domain = AsyncCustomDomain(public_domain="sbx.tu-dominio.com")
        await domain.deploy(certificate_arn="arn:aws:acm:us-east-1:<cuenta>:certificate/<id>")

        async with await AsyncSandbox.create("rayito-base", allowed_ports=[8000]) as sbx:
            jwe = (await sbx.get_host(8000)).headers["x-aws-proxy-auth"]
            route = await domain.register(
                sbx.sandbox_id, 8000, endpoint=sbx.endpoint, jwe=jwe,
                traffic_token=secrets.token_urlsafe(32), ttl_seconds=2400,
            )
            print(route.host)
            await domain.unregister(sbx.sandbox_id, 8000)


    asyncio.run(main())
    ```

=== "TypeScript"

    ```typescript
    import { randomBytes } from "node:crypto";
    import { CustomDomain, Sandbox } from "rayito";

    const domain = new CustomDomain({ publicDomain: "sbx.tu-dominio.com" });
    await domain.deploy({ certificateArn: "arn:aws:acm:us-east-1:<cuenta>:certificate/<id>" });

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
      console.log(route.host);
      await domain.unregister(sbx.sandboxId, 8000);
    } finally {
      await sbx.kill();
    }
    ```

## Cómo funciona

1. La distribución tiene un único origen "placeholder" en la plantilla —
   nunca se contacta de verdad.
2. Cada petición pasa primero por la CloudFront Function
   (`infra/functions/custom_domain_router.js`), que borra cualquier
   cabecera `x-aws-proxy-*` que el viewer haya mandado, lee la ruta del
   `KeyValueStore` por el hostname (`{puerto}-{alias}`), comprueba el
   `traffic_token` si la ruta no es pública, y llama a
   `cf.updateRequestOrigin()` con el `endpoint` real del sandbox y las
   cabeceras del proxy que hacen falta.
3. Una ruta sin entrada en el `KeyValueStore`, o cuyo `ttl_seconds` ya
   pasó, recibe 404 directo de la Function: nunca llega al origen
   placeholder, y desde fuera no se distingue de una ruta que nunca
   existió.

## Limitaciones conocidas

- Sin `Sandbox.create(domain=)`/`expose()`/`get_host()` cableados, hay que
  acuñar el JWE y registrar la ruta a mano (como en el ejemplo).
- `register()` exige `traffic_token` salvo que pases `public=True` a
  propósito — nunca hay una ruta pública por omisión. El `traffic_token`
  sólo se guarda como su hash sha256 — no hay forma de recuperarlo después
  de perderlo; genera uno nuevo y vuelve a registrar la ruta.
- Una ruta no se borra sola cuando el sandbox muere: llama a
  `unregister()` tú mismo. Sin eso, la ruta se acota sola por dos lados
  independientes: `ttl_seconds` de `register()`/`refresh()` (`m.x`, que la
  propia CloudFront Function comprueba — una ruta caducada da 404, igual
  que una que nunca existió) y la caducidad del JWE en sí, del lado del
  proxy de AWS (401/403), lo primero que llegue.
- Los puertos `8080` y `9000` (puerto de hooks y reservado) no se pueden
  exponer, igual que en el resto del SDK.
