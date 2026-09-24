## ADDED Requirements

### Requirement: SECURITY.md documents the in-guest egress policy (T8 and T17)
`SECURITY.md` T8 SHALL state:
- the in-guest egress policy on `rayito-base-caps` (routes for uid 1000–65535 and the local proxy);
- that the default image fails closed (the SDK terminates the MicroVM and raises `UnimplementedError`);
- that the customer VPC connector of `infra/egress-connector.yaml` remains the only out-of-guest control, and that security groups do not filter Amazon DNS.

A new threat row T17 "Política de egress en el guest y proxy local de rayd" SHALL state:
- that the local proxy runs as root and is an SSRF surface, closed by the guard: loopback, link-local including `169.254.169.254` and `fd00:ec2::254`, own interface addresses, unspecified, multicast and broadcast, checked after resolution and dialled at the checked address only;
- that in-guest layers fall to a guest-kernel exploit, to root in the guest (`RAYITO_ALLOW_ROOT`) and to the exempt platform agent uids 991–994;
- that DNS resolution for uid ≥ 1000 under deny-all on `rayito-base-caps` is a known residual risk (DNS exfiltration through the in-guest platform resolvers) while every connection outside the VM fails, that in-guest enforcement is best-effort with the VPC connector as the hard control, and that the proxy never resolves a denied name under a deny-by-default policy;
- that proxy-unaware clients fail closed;
- that upstream proxy credentials travel only in the token-authenticated `UpdateNetwork`, are zeroized, never logged, never echoed and never placed in the `runHookPayload`;
- that any process can use the proxy but only within the policy;
- that the IMDS rule of C-05's family and the deferred C-01 are not regressed.

`docs/site/docs/security.md` SHALL carry a one-line T17 summary.

#### Scenario: T17 names the guard and the residual risks
- **WHEN** a reviewer reads the T8 and T17 rows of `SECURITY.md`
- **THEN** T8 names `rayito-base-caps`, the fail-closed default image and the VPC connector with its DNS caveat, and T17 names `169.254.169.254`, the own-address rule, the guest-kernel and guest-root residuals, the DNS behaviour, the fail-closed proxy-unaware clients and the credential handling
