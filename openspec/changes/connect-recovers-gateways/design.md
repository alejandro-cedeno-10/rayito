## Decisions

1. **Reuse `ConfigureStatus`.** `SecretGatewayStatus` already reports each
   route's name, real port and last error class, which is all `sbx.gateways`
   and `sbx.agent` need (`GatewayStatus.url`). No new RPC or field.
2. **Never overwrite a handle that applied `gateways=`.** Only that handle
   holds the `SecretGateway` definitions and can rotate values, so
   `connect()` on it keeps it as is.
3. **Recovered `refresh()` is read-only.** Without the definitions there is
   nothing to resend; refreshing re-reads the status (ports, errors). The
   docs say rotation belongs to the creating process or `reincarnate()`.
4. **Gate on feature flags.** Recovery runs only when `Health.features` has
   `configure` and `secret_gateway`; older agents get no extra call.
5. **Layering.** `gateways_recoverable`/`gatewaysRecoverable` and
   `recovered_gateways`/`recoveredGateways` live next to `GatewayHandle` in
   the section adapter; the sandboxes only wire the `ConfigureStatus` reader.
