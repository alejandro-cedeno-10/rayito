# Red saliente

Por defecto un sandbox sale a internet por el conector gestionado
`INTERNET_EGRESS`, y **omitir el conector no cierra la red**: un MicroVM
lanzado sin `egressNetworkConnectors` (o con `[]`) hereda el de la versión de
imagen, y `NO_EGRESS` no existe (`AWS_API_NOTES.md` §2, Q44 y Q60). Desde M9
(`m9-egress-policy`, ADR-012) Rayito aplica la política de egress de E2B
**dentro del guest**, en la variante `rayito-base-caps`: `rayd` (root, con
`CAP_NET_ADMIN`) instala rutas por uid para el código del sandbox y, para las
reglas por nombre de host, un proxy local.

!!! warning "Sólo `rayito-base-caps`, y falla cerrado"
    En cualquier otra imagen pedir una política (`network=` con `deny_out` o
    `egress_proxy`, o `allow_internet_access=False`) hace que el SDK termine
    el VM y lance `UnimplementedError` antes de devolverte un sandbox: nunca
    recibes uno con la red abierta creyendo que estaba cerrada. Esto vale
    también con `keep_on_failure=True`. `get_health().egress_enforcement` dice
    cómo se aplica (`none`, `guest_routes`, `guest_routes_and_proxy`).

!!! note "DNS bajo deny-all"
    En `rayito-base-caps` los resolvedores DNS de la plataforma escuchan
    **dentro** del guest, así que con `allow_internet_access=False` (o
    cualquier deny-all) un nombre **puede seguir resolviéndose**, pero
    **ninguna conexión sale del VM**: `getaddrinfo` puede devolver una
    dirección y el `connect` a ella falla. Es un riesgo residual conocido (un
    canal de exfiltración por consultas DNS, `SECURITY.md` T17) y una
    decisión escrita: la adenda de ADR-012, **opción C**, acepta en M9 que un
    nombre resuelva siempre que ninguna dirección resuelta conecte. Bloquear
    el DNS de uid ≥ 1000 bajo deny-all (la **opción A**, una regla `ip rule`
    para el puerto 53 antes de la regla `local`) queda para el siguiente
    ciclo. Si necesitas cerrarlo hoy, usa además el conector VPC de
    [la alternativa de plataforma](#la-alternativa-de-plataforma).

## Modos y semántica

La semántica es la de E2B: una entrada permitida **gana siempre** a una
denegada, y sin `deny_out` ni `egress_proxy` no se restringe nada (`allow_out`
solo no cierra la red).

| Política | Modo | Qué hace `rayd` |
|---|---|---|
| sin `deny_out` ni `egress_proxy` | sin restricción | nada |
| `deny_out` con CIDR/IP (y `allow_out` con CIDR/IP) | rutas | `blackhole` por uid 1000–65535 de lo denegado menos lo permitido |
| nombres de host en `allow_out`, o `egress_proxy` | sólo proxy | todo el tráfico directo bloqueado; sólo sale lo que pasa por el proxy local |

- Entradas: un CIDR (`10.0.0.0/8`), una IP, `ALL_TRAFFIC` (`0.0.0.0/0`, que
  cubre IPv4 e IPv6), un nombre exacto (`api.example.com`) o `*.dominio`
  (cualquier subdominio a cualquier profundidad, **nunca** el propio
  dominio). Los nombres de host sólo valen en `allow_out`.
- Un selector puede ser una función que recibe el contexto de E2B
  (`ctx.all_traffic`).
- Máximo 256 entradas por lista y 64 nombres de host.

## Ejemplos

=== "Python"

    ```python
    from rayito import ALL_TRAFFIC, EgressProxy, Sandbox

    # Sin internet (deny-all): ninguna conexión sale del VM (los nombres aún
    # pueden resolverse, ver «DNS bajo deny-all»).
    sbx = Sandbox.create("rayito-base-caps", allow_internet_access=False)

    # Sólo la red interna y un servicio por nombre.
    sbx = Sandbox.create(
        "rayito-base-caps",
        network={
            "deny_out": [ALL_TRAFFIC],
            "allow_out": ["10.0.0.0/8", "api.example.com", "*.pypi.org"],
        },
    )

    # Cambiarla en caliente (afecta a las conexiones nuevas) y leerla.
    state = sbx.update_network({"deny_out": [ALL_TRAFFIC], "allow_out": ["api.example.com"]})
    print(state.enforcement, state.local_proxy_port)
    print(sbx.get_network().allow_out)

    # Encadenar al proxy SOCKS5 del operador (credenciales sólo por RPC).
    sbx.update_network({"egress_proxy": EgressProxy("proxy.interno:1080", "usuario", "secreto")})

    # Forma de clase, sin handle (necesita el access token).
    Sandbox.update_network(sbx.sandbox_id, {"deny_out": [ALL_TRAFFIC]}, access_token=sbx.access_token)
    ```

=== "Python (async)"

    ```python
    import asyncio

    from rayito import ALL_TRAFFIC, AsyncSandbox


    async def main() -> None:
        async with await AsyncSandbox.create(
            "rayito-base-caps", network={"deny_out": [ALL_TRAFFIC], "allow_out": ["api.example.com"]}
        ) as sbx:
            state = await sbx.update_network({"deny_out": [ALL_TRAFFIC]})
            print(state.enforcement, (await sbx.get_network()).deny_out)
            await sbx.update_network(None)      # sin restricciones otra vez


    asyncio.run(main())
    ```

=== "TypeScript"

    ```ts
    import { ALL_TRAFFIC, Sandbox } from "rayito";

    await using sbx = await Sandbox.create({
      template: "rayito-base-caps",
      network: { denyOut: [ALL_TRAFFIC], allowOut: ["api.example.com", "*.pypi.org"] },
    });
    const state = await sbx.updateNetwork({ denyOut: [ALL_TRAFFIC], allowOut: ["10.0.0.0/8"] });
    console.log(state.enforcement, state.localProxyPort);
    console.log((await sbx.getNetwork()).allowOut);

    await sbx.updateNetwork({
      egressProxy: { address: "proxy.interno:1080", username: "usuario", password: "secreto" },
    });

    // Sin internet desde el create, y la forma estática con el access token
    await using offline = await Sandbox.create({
      template: "rayito-base-caps",
      allowInternetAccess: false,
    });
    await Sandbox.updateNetwork(offline.sandboxId, undefined, {
      accessToken: offline.accessToken,
      allowInternetAccess: false,
    });
    ```

=== "Shim de E2B"

    ```python
    from rayito.e2b import ALL_TRAFFIC, Sandbox

    with Sandbox.create("rayito-base-caps", allow_internet_access=False) as sbx:
        sbx.update_network({"allow_out": ["api.example.com"], "deny_out": [ALL_TRAFFIC]})
    ```

## Cómo llega el proxy a tus procesos

Cuando la política necesita el proxy, `rayd` lo arranca en `127.0.0.1` y
exporta `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY` (`socks5h://`) y `NO_PROXY`
(también en minúsculas) a cada proceso, PTY y kernel que lance **después**.
Tus `envs` pueden sobrescribirlas, pero el tráfico directo sigue bloqueado
por las rutas. Un proceso, una PTY o un kernel arrancados antes de que el
proxy existiera (sólo pasa si la primera política restrictiva llega por un
`update_network` posterior) no tienen las variables: las rutas les aplican,
las reglas por nombre no. Para cubrir el kernel por defecto, pasa `network=`
en `create()`.

## Diferencias con E2B

- **Los clientes que no honran el proxy fallan cerrados** en modo sólo-proxy
  (sockets en crudo, algunos runtimes, `git://`, `ssh`); E2B filtra de forma
  transparente fuera del VM.
- Las reglas por nombre de host sólo valen en los puertos 80 y 443 y a través
  del proxy.
- El DNS de uid ≥ 1000 no se bloquea: los resolvedores de la plataforma
  escuchan dentro del guest (medido, fila Q66, QE1), así que bajo deny-all
  (`allow_internet_access=False` incluido) un nombre puede resolverse aunque
  ninguna conexión salga. El proxy resuelve por ti los nombres permitidos y
  nunca consulta uno denegado.
- UDP y QUIC no pasan por el proxy.
- Un cambio de política afecta a las conexiones nuevas; las que ya existían
  pueden seguir.
- Root dentro del guest no se filtra (sólo uid 1000–65535, y root exige
  `RAYITO_ALLOW_ROOT`), ni los uids del agente de la plataforma.
- `network.https_ports` es `UnimplementedError` hasta medir si el proxy
  reenvía TLS a un puerto del guest (fila Q67, QE2); `network.rules`,
  `mask_request_host` y `allow_public_traffic=True` no tienen primitiva
  ([Compatibilidad con E2B](e2b-compat.md)).
- Sólo en `rayito-base-caps`.

## La alternativa de plataforma

El único control **fuera** del guest es un conector VPC propio con un
security group deny-all (`infra/egress-connector.yaml`, receta en
`infra/README.md`), pasado con `Sandbox.create(egress=[<arn>])`. No depende
de la imagen ni de root dentro del VM, pero exige una VPC propia y un
security group no filtra el DNS de Amazon. Las dos capas se pueden combinar.
Las capas del guest no resisten a un exploit del kernel del guest ni a root
dentro del VM (`SECURITY.md` T17).
