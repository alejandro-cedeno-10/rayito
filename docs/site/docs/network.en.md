# Outbound network

By default a sandbox reaches the internet through the managed
`INTERNET_EGRESS` connector, and **omitting the connector does not close
the network**: a MicroVM launched without `egressNetworkConnectors` (or with
`[]`) inherits the one from the image version, and `NO_EGRESS` does not
exist (measured). Rayito applies E2B's egress policy **inside the guest**,
in the `rayito-base-caps` variant: `rayd` (root, with `CAP_NET_ADMIN`)
installs per-uid routes for the sandbox code and, for hostname rules, a
local proxy.

!!! warning "Only `rayito-base-caps`, and it fails closed"
    On any other image, requesting a policy (`network=` with `deny_out` or
    `egress_proxy`, or `allow_internet_access=False`) makes the SDK
    terminate the VM and raise `UnimplementedError` before handing you a
    sandbox: you never receive one with the network open thinking it was
    closed. This also holds with `keep_on_failure=True`.
    `get_health().egress_enforcement` tells you how it is applied (`none`,
    `guest_routes`, `guest_routes_and_proxy`).

!!! note "DNS under deny-all"
    On `rayito-base-caps` the platform's DNS resolvers listen **inside** the
    guest, on addresses that the kernel's `local` table resolves before any
    policy rule. In 0.3.0 and 0.3.1 that was a known residual risk: a name
    could resolve while no real connection left the VM. Since 0.3.2, under
    deny-all `rayd` moves the `local` rule to priority 1 and installs, at the
    priority it frees up, an `ip rule ... uidrange 1000-65535 ipproto
    udp/tcp dport 53 prohibit` rule — an atomic change, with rollback if it
    fails halfway and no window without local routing. With that,
    `getaddrinfo` as uid 1000 also fails under deny-all, not just the
    `connect`. It is still guest-side enforcement, best effort (it falls to
    a guest kernel exploit or to root with `RAYITO_ALLOW_ROOT`,
    `SECURITY.md` T17); for control outside the guest also use the VPC
    connector from
    [the platform alternative](#la-alternativa-de-plataforma) (with its own
    caveat: a security group does not filter Amazon's DNS).

## Modes and semantics { #modos-y-semantica }

The semantics are E2B's: an allowed entry **always wins** over a denied
one, and without `deny_out` or `egress_proxy` nothing is restricted
(`allow_out` alone does not close the network).

| Policy | Mode | What `rayd` does |
|---|---|---|
| no `deny_out` or `egress_proxy` | unrestricted | nothing |
| `deny_out` with CIDR/IP (and `allow_out` with CIDR/IP) | routes | per-uid 1000-65535 `blackhole` of what is denied minus what is allowed |
| hostnames in `allow_out`, or `egress_proxy` | proxy only | all direct traffic blocked; only what goes through the local proxy gets out |

- Entries: a CIDR (`10.0.0.0/8`), an IP, `ALL_TRAFFIC` (`0.0.0.0/0`, which
  covers IPv4 and IPv6), an exact name (`api.example.com`) or `*.domain`
  (any subdomain at any depth, **never** the domain itself). Hostnames are
  only valid in `allow_out`.
- A selector can be a function that receives E2B's context
  (`ctx.all_traffic`): `network={"deny_out": lambda ctx: [ctx.all_traffic]}`.
- `network=` accepts a `dict` with E2B's keys (`allow_out`, `deny_out`,
  `egress_proxy`) or an already-resolved `NetworkPolicy(allow_out=...,
  deny_out=..., egress_proxy=...)`; in TypeScript, an object with
  `allowOut`, `denyOut` and `egressProxy`.
- Cap on entries per list and on hostnames: see [Limits](limits.md).

## Examples { #ejemplos }

=== "Python"

    ```python
    import os

    from rayito import ALL_TRAFFIC, EgressProxy, Sandbox

    # No internet (deny-all): no connection leaves the VM, and on
    # rayito-base-caps names do not resolve either (see "DNS under deny-all").
    with Sandbox.create("rayito-base-caps", allow_internet_access=False) as offline:
        print(offline.get_network().deny_out)

    # Only the internal network and one service by name.
    with Sandbox.create(
        "rayito-base-caps",
        network={
            "deny_out": [ALL_TRAFFIC],
            "allow_out": ["10.0.0.0/8", "api.example.com", "*.pypi.org"],
        },
    ) as sbx:
        # Change it live (it affects new connections) and read it.
        state = sbx.update_network({"deny_out": [ALL_TRAFFIC], "allow_out": ["api.example.com"]})
        print(state.enforcement, state.local_proxy_port)
        print(sbx.get_network().allow_out)

        # Chain to the operator's SOCKS5 proxy (credentials only over RPC).
        proxy = EgressProxy("proxy.internal:1080", "user", os.environ["PROXY_PASSWORD"])
        sbx.update_network({"egress_proxy": proxy})

        # Class form, without a handle (needs the access token).
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
            await sbx.update_network(None)      # unrestricted again


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
      egressProxy: {
        address: "proxy.internal:1080",
        username: "user",
        password: process.env.PROXY_PASSWORD ?? "",
      },
    });

    // No internet from create, and the static form with the access token
    await using offline = await Sandbox.create({
      template: "rayito-base-caps",
      allowInternetAccess: false,
    });
    await Sandbox.updateNetwork(offline.sandboxId, undefined, {
      accessToken: offline.accessToken,
      allowInternetAccess: false,
    });
    ```

=== "Shim E2B"

    ```python
    from rayito.e2b import ALL_TRAFFIC, Sandbox

    with Sandbox.create("rayito-base-caps", allow_internet_access=False) as sbx:
        sbx.update_network({"allow_out": ["api.example.com"], "deny_out": [ALL_TRAFFIC]})
    ```

`update_network()` replaces the whole policy (it does not merge it with the
previous one) and `update_network(None)` removes it;
`allow_internet_access=False` adds `ALL_TRAFFIC` to `deny_out`. Both, and
`get_network()`, return a `NetworkState`: `allow_out`, `deny_out`,
`egress_proxy_configured` (never the proxy's address or credentials),
`enforcement` and `local_proxy_port` (TypeScript: `allowOut`, `denyOut`,
`egressProxyConfigured`, `localProxyPort`). In the E2B shim,
`update_network()` returns `None`, like E2B.

## How the proxy reaches your processes { #como-llega-el-proxy-a-tus-procesos }

When the policy needs the proxy, `rayd` starts it on `127.0.0.1` and
exports `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY` (`socks5h://`) and
`NO_PROXY` (also in lowercase) to every process, PTY and kernel it launches
**afterwards**. Your `envs` can override them, but direct traffic stays
blocked by the routes. A process, PTY or kernel started before the proxy
existed (this only happens if the first restrictive policy arrives through
a later `update_network`) does not have the variables: the routes apply to
them, the name rules do not. To cover the default kernel, pass `network=` in
`create()`.

## Differences from E2B { #diferencias-con-e2b }

- **Clients that do not honor the proxy fail closed** in proxy-only mode
  (raw sockets, some runtimes, `git://`, `ssh`); E2B filters transparently
  outside the VM.
- Hostname rules only apply on ports 80 and 443 and through the proxy. In
  an absolute-form `http://` request (what a client with `HTTP_PROXY`
  does), the proxy rewrites the `Host` header with the checked name; a
  `CONNECT` or SOCKS5 tunnel to an allowed name passes as is, so it can
  carry another `Host` header (on 80) or another SNI (on 443) toward the
  same IP addresses. It is the same residual risk as shared IPs (CDN) on
  both ports (`SECURITY.md` T17).
- Under deny-all on `rayito-base-caps` DNS for uid ≥ 1000 is blocked: the
  platform's resolvers still listen inside the guest (measured), but an
  `ip rule` for port 53 intercepts them before the `local` table resolves
  them. Outside deny-all (a partial policy in routes mode) DNS is not
  touched: the proxy resolves allowed names for you and never queries a
  denied one.
- UDP and QUIC do not go through the proxy.
- A policy change affects new connections; the ones that already existed
  may continue.
- Root inside the guest is not filtered (only uid 1000-65535, and root
  requires `RAYITO_ALLOW_ROOT`), nor are the platform agent's uids.
- `network.https_ports` is `UnimplementedError`: measured, the proxy does
  not forward end-to-end TLS to a guest port; `network.rules`,
  `mask_request_host` and `allow_public_traffic=True` have no primitive
  ([E2B compatibility](e2b-compat.md)).
- Only on `rayito-base-caps`.

## The platform alternative { #la-alternativa-de-plataforma }

The only control **outside** the guest is a VPC connector of your own with
a deny-all security group (`infra/egress-connector.yaml`, recipe in
`infra/README.md`), passed with `Sandbox.create(egress=[<arn>])`. It does
not depend on the image or on root inside the VM, but it requires a VPC of
your own and a security group does not filter Amazon's DNS. The two layers
can be combined. The guest layers do not withstand a guest kernel exploit
or root inside the VM (`SECURITY.md` T17).

??? info "Sources and measurements"
    - Design: ADR-012 and its addendum (DNS under deny-all) in
      [`ARCHITECTURE.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/ARCHITECTURE.md);
      threat model T17 in [`SECURITY.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/SECURITY.md).
    - Measurements in [`AWS_API_NOTES.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/AWS_API_NOTES.md):
      §2, Q44 and Q60 (omitting the connector does not close the network),
      Q66 (DNS resolvers inside the guest) and Q67 (`https_ports`).
