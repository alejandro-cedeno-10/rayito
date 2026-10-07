## MODIFIED Requirements

### Requirement: register_webhook only accepts URLs the deliverer can reach
`LifecycleEvents.register_webhook`/`registerWebhook` SHALL reject with `InvalidArgumentException`/`InvalidArgumentError`, before any AWS call and without echoing the URL, any URL that is not `https://`, has no host, has a port outside 1–65535, has a DNS host whose ASCII form has a label outside 1–63 characters of `[A-Za-z0-9_-]` or exceeds 253 characters, or contains whitespace, control characters or backslashes. It SHALL also reject a host that is `localhost` or ends in `.localhost` (with or without a trailing dot, in any case), and a literal IP address (IPv4 in any form the WHATWG URL parser accepts, such as `0x7f.1` or `2130706433`; IPv6; or an IPv4-mapped IPv6 address, judged as the IPv4 it carries) that the deliverer's SSRF guard would block: loopback, private, link-local (including the IMDS address), multicast, reserved, unspecified or CGNAT (100.64.0.0/10). A DNS name SHALL NOT be resolved at registration; blocking a name that resolves (or later re-resolves) to such an address remains the deliverer's job. Both SDKs SHALL agree on `testdata/lifecycle-events/webhook-url-vectors.json`, and both SDKs and the deliverer's SSRF guard SHALL give the same verdict on every address of `testdata/lifecycle-events/ssrf-address-vectors.json`.

#### Scenario: an unreachable URL is never stored
- **WHEN** `register_webhook("https://h:99999/", ...)` is called
- **THEN** `InvalidArgumentException` is raised, its message does not contain the URL, and no row is written

#### Scenario: a metadata or loopback target is refused at the client
- **WHEN** `register_webhook("https://169.254.169.254/latest/meta-data/", ...)`, `register_webhook("https://[::ffff:127.0.0.1]/hook", ...)` or `register_webhook("https://localhost/hook", ...)` is called
- **THEN** `InvalidArgumentException` is raised, its message does not contain the URL, and no row is written

#### Scenario: client and deliverer classify the same addresses
- **WHEN** every address of `ssrf-address-vectors.json` is checked by `is_blocked_webhook_address`, `isBlockedWebhookAddress` and the deliverer's `is_blocked`
- **THEN** the three give the vector's verdict, and `https://<address>/hook` is accepted by `register_webhook` exactly when the address is in `allowed`
