## Why

The 0.8.0 regression review found that `register_webhook`/`registerWebhook`
only rejects non-HTTPS or malformed URLs. A webhook pointing at
`https://169.254.169.254/…`, `https://127.0.0.1/…` or `https://localhost/…`
is stored and only refused later, at every delivery attempt, by the
deliverer's SSRF guard. Refusing it at the client is defence in depth: the
mistake surfaces where it is made, and a guard regression in the deliverer
would not be the only line.

## What Changes

- Both SDKs reject, before any AWS call, a webhook URL whose host is
  `localhost`/`*.localhost` or a literal IP the deliverer's SSRF guard
  blocks (loopback, private, link-local/IMDS, multicast, reserved,
  unspecified, CGNAT), including IPv4-mapped IPv6 and the WHATWG numeric
  IPv4 forms (`0x7f.1`, `2130706433`).
- New shared vectors `testdata/lifecycle-events/ssrf-address-vectors.json`,
  used by the deliverer's `test_ssrf.py` (replacing its inline lists) and by
  both SDKs, so the three classifiers cannot drift.
- `webhook-url-vectors.json` gains the new rejected URLs; its two accepted
  literal IPs move from documentation ranges (`192.0.2.0/24`,
  `2001:db8::/32`, which the deliverer already blocks) to public ones.
- `INVALID_WEBHOOK_URL` says the URL must reach a public address.
- DNS names are still not resolved at registration: DNS rebinding stays the
  deliverer's job.

## Capabilities

### Modified Capabilities

- `lifecycle-events`: "register_webhook only accepts URLs the deliverer can
  reach" also covers localhost and non-public literal addresses.

## Impact

- Python: `rayito/_lifecycle_events/_domain.py`; TypeScript:
  `src/lifecycle-events/domain.ts`; deliverer tests only (its code does not
  change).
- Behaviour change: a URL with such a host that used to be stored (and never
  delivered) now raises `InvalidArgumentException`/`InvalidArgumentError`.
- No AWS cost, no runtime or infra change.
