## Why

`GatewayException` (Python) and `GatewayError` (TypeScript) are public but
no code path raises them. Everything the secrets gateway refuses per request
reaches the sandbox process as an HTTP response (403, 429, 502, 504), and a
config `rayd` rejects already surfaces as `SandboxException`/`SandboxError`
from `create()`/`take()`/`refresh()`. A public type that is never raised
misleads callers into catching it. It will not ship in 0.9.0.

## What Changes

- **BREAKING**: remove `GatewayException` from `rayito` and
  `rayito.exceptions`, and `GatewayError`/`GatewayErrorOptions` from the
  TypeScript package. No deprecation period.
- Docs (`errores.md`, the secrets-gateway page, the TypeScript reference)
  and both CHANGELOGs drop it, with migration notes.

## Capabilities

### Modified Capabilities

- `secret-gateway`: the SDKs expose no gateway-specific exception.

## Impact

Python and TypeScript SDK public surface; docs. No proto, rayd or AWS change.
