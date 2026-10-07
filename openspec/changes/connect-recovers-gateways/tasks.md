## 1. SDKs

- [x] 1.1 Python: `gateways_recoverable`, `recovered_gateways` and `_recover_gateways` in `Sandbox`/`AsyncSandbox` `connect()` (static and instance)
- [x] 1.2 TypeScript: `gatewaysRecoverable`, `recoveredGateways` and `#recoverGateways` in `Sandbox.connect()` (static and instance)
- [x] 1.3 Unit tests in both SDKs
- [x] 1.4 Review: tests through the real `connect()` (static and instance, sync and async), close on a failed read, connect request timeout, `owns_gateways`/`ownsGateways` so a recovered handle re-reads
- [x] 1.5 A token rejected by the recovery read leaves `connect()` lazy about authentication (AWS regression run)

## 2. Docs

- [x] 2.1 Agent guide: "Usar el agente desde otro proceso"
- [x] 2.2 CHANGELOG `[Unreleased]` in both packages
