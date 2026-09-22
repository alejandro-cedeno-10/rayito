# egress-control Specification

## Purpose
TBD - created by archiving change m6-hardening. Update Purpose after archive.
## Requirements
### Requirement: SandboxInfo reports the connectors a sandbox got
`SandboxInfo` SHALL carry `ingress` and `egress` as tuples of connector ARNs read from `ingressNetworkConnectors` and `egressNetworkConnectors` of `run-microvm` and `get-microvm` (empty when absent); `SandboxListItem` SHALL stay unchanged (list items do not carry them). `Sandbox.create(egress=...)` SHALL keep accepting managed names and own ARNs, at most 10, resolved by `connector_arns`.

#### Scenario: egress echoed
- **WHEN** the e2e creates a sandbox with `egress=[arn]` and calls `get_info()`
- **THEN** `info.egress == (arn,)` and `info.ingress` lists the managed `ALL_INGRESS` ARN the platform attached by default

#### Scenario: stubbed get-microvm
- **WHEN** the unit test stubs `get-microvm` with both connector lists
- **THEN** `SandboxInfo.ingress` and `.egress` hold them in order, and a response without the fields yields empty tuples

### Requirement: The egress allowlist recipe ships as a validated CloudFormation template
`infra/egress-connector.yaml` SHALL define an `AWS::Lambda::NetworkConnector` whose `Configuration.VpcEgressConfiguration` uses exactly the documented properties (`AssociatedComputeResourceTypes: [MicroVm]`, `NetworkProtocol: IPv4`, `SecurityGroupIds`, `SubnetIds`), an `OperatorRole` trusted by `lambda.amazonaws.com` with the ENI permissions (`ec2:CreateNetworkInterface`, `ec2:DescribeNetworkInterfaces`, `ec2:DeleteNetworkInterface`, `ec2:DescribeSubnets`, `ec2:DescribeSecurityGroups`, `ec2:DescribeVpcs`, `ec2:AssignPrivateIpAddresses`, `ec2:UnassignPrivateIpAddresses`), a security group with no ingress and an egress allowlist built from the `AllowedCidrs`/`AllowedPort` parameters (default: nothing allowed), and outputs `ConnectorArn` plus the `lambda:PassNetworkConnector` statement the caller needs. `make infra-lint` SHALL run `aws cloudformation validate-template` and `cfn-lint` on it (E3006 ignored only for the connector type when cfn-lint's spec predates it, version recorded). `spike/m0/iam.yaml` `CallerPolicy` SHALL accept a `NetworkConnectorArns` list parameter for `lambda:PassNetworkConnector`. `infra/README.md` SHALL document the recipe (`Sandbox.create(egress=[ConnectorArn])`), that reaching an internet destination additionally needs a NAT path the customer owns, and the deploy gate.

#### Scenario: template validates
- **WHEN** `make infra-lint` runs
- **THEN** `validate-template` succeeds and `cfn-lint` reports no error other than an ignored E3006 for `AWS::Lambda::NetworkConnector`

### Requirement: The connector is deployed in acceptance only behind a cost and VPC gate
The acceptance SHALL deploy the template (stack `rayito-egress-e2e`, deny-all default) and run `test_egress_allowlist` only when the account's region has a VPC with at least one subnet that belongs to this project or was explicitly lent by its owner (the stack creates a security group and ENIs inside that VPC; another workload's network is not used unasked) and the Lambda MicroVMs pricing section lists no charge for network connectors; the deny-all default needs no NAT gateway, so a missing NAT SHALL NOT be a reason to skip. The test SHALL assert that `urllib.request.urlopen("https://example.com", timeout=5)` fails (non-zero exit within 10 s) in a sandbox created with `egress=[ConnectorArn]` and succeeds in a default sandbox, and SHALL record whether the platform still attaches `INTERNET_EGRESS` next to an own connector (Q42). Otherwise the test SHALL be skipped by the missing `RAYITO_EGRESS_CONNECTOR_ARN`, Q42 recorded as not measured with the reason, and the stack SHALL never be created. The stack SHALL be deleted in teardown when it was created.

#### Scenario: allowlist enforced
- **WHEN** the gate passes and the e2e runs with `RAYITO_EGRESS_CONNECTOR_ARN`
- **THEN** the sandbox with the connector cannot open `https://example.com`, the default sandbox can, `get_info().egress` holds the connector ARN, and the stack is gone after the session

#### Scenario: gate fails
- **WHEN** no VPC with a subnet exists, the only VPC belongs to another workload and was not lent, or the pricing page lists a connector charge
- **THEN** `test_egress_allowlist` is skipped, no CloudFormation stack is created, `AWS_API_NOTES.md` Q46 (the design's Q42) says "not measured" with the reason, and `SECURITY.md` T8 states that the allowlist is validated (`make infra-lint`) but not measured

