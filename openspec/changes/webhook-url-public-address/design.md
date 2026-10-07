## Context

The deliverer (`infra/lambdas/events_webhooks/domain/ssrf.py`) classifies
every resolved address with `ipaddress` predicates plus CGNAT and maps an
IPv4-mapped IPv6 address to its IPv4 first. The SDKs only validated URL
shape. The deliverer is packaged on its own (no `rayito` dependency), and
the TypeScript SDK has no `ipaddress`, so one shared implementation is not
possible; one shared set of vectors is.

## Decisions

- **D1 Same rule, three implementations, one vector file.** Python
  `is_blocked_webhook_address` is the deliverer's `is_blocked` line for
  line (same predicates). TypeScript `isBlockedWebhookAddress` carries the
  CIDR tables of CPython 3.12's `ipaddress` (`_private_networks` minus
  `_private_networks_exceptions`, plus loopback, link-local, multicast,
  reserved, unspecified) and CGNAT, because the deliverer runs on
  `python3.12`. `ssrf-address-vectors.json` is read by all three test
  suites; it avoids the few ranges whose `is_private` verdict changed
  between CPython patch releases, so the SDK's own Python (3.11+) agrees.
- **D2 Literal addresses only.** The SDK never resolves DNS at
  registration: a resolution there proves nothing at delivery time (DNS
  rebinding) and would add a network call to a pure validation. The
  deliverer keeps resolving and classifying every attempt.
- **D3 `localhost`.** `localhost` and any name under `.localhost` (RFC 6761
  §6.3, case-insensitive, optional trailing dot) are refused: they can only
  resolve to loopback.
- **D4 Numeric IPv4 forms.** The WHATWG `URL` (TypeScript) turns a host that
  ends in a number into an IPv4 (`0x7f.1` → `127.0.0.1`) or fails, and
  `getaddrinfo` in the deliverer accepts the same forms. Python's
  `urlsplit` does not, so `_domain.py` implements the WHATWG IPv4 parser
  (1–4 decimal, octal or hex parts) to keep both SDKs on the same verdict;
  a host that ends in a number but is not a valid IPv4 (`example.123`,
  `1.2.3.256`) is rejected, as WHATWG does.
- **D5 One message.** `INVALID_WEBHOOK_URL` grows to name the public-address
  requirement instead of adding a second constant; it still never repeats
  the URL.
