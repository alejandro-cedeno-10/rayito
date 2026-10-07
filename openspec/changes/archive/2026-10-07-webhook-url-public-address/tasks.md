## 1. Vectors

- [x] 1.1 `testdata/lifecycle-events/ssrf-address-vectors.json` with the deliverer's blocked/allowed addresses plus private, reserved, ULA, multicast and NAT64 cases
- [x] 1.2 `infra/lambdas/events_webhooks/tests/test_ssrf.py` reads it instead of inline lists
- [x] 1.3 `webhook-url-vectors.json`: localhost, literal private/loopback/link-local/IMDS/CGNAT URLs, IPv4-mapped IPv6 and WHATWG numeric forms rejected; public literal IPs accepted

## 2. Python

- [x] 2.1 `is_blocked_webhook_address`, WHATWG IPv4 parsing and the `localhost` check in `_lifecycle_events/_domain.py`
- [x] 2.2 Tests: shared vectors, sync and async `register_webhook` refuse the URLs without echoing them

## 3. TypeScript

- [x] 3.1 `isBlockedWebhookAddress`/`parseIpAddress` and the `localhost` check in `src/lifecycle-events/domain.ts`
- [x] 3.2 Tests: shared vectors and `registerWebhook`

## 4. Docs

- [x] 4.1 `eventos-y-webhooks.md`: what the client refuses, what stays the deliverer's job, reference and error tables
- [x] 4.2 CHANGELOG of both SDKs
