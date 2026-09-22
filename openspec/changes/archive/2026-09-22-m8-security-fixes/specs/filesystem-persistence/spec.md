## MODIFIED Requirements

### Requirement: Execution role policy scoped to the persistence prefix
`spike/m0/iam.yaml` SHALL accept `PersistenceBucket` (default empty) and `PersistencePrefix` (default `rayito-home`, pattern `^[A-Za-z0-9_.-][A-Za-z0-9_./-]*[A-Za-z0-9_.-]$|^[A-Za-z0-9_.-]$`, which accepts no `*`, `!`, `'`, `(` or `)` and no leading or trailing slash) and, only when the bucket is set, attach to `ExecutionRole` an inline policy `persistence` allowing `s3:PutObject`, `s3:GetObject` and `s3:AbortMultipartUpload` on `arn:aws:s3:::<bucket>/<prefix>/*` and `s3:ListBucket` on `arn:aws:s3:::<bucket>` with `Condition: StringLike: s3:prefix: <prefix>/*`, plus a statement `NeverTheImageArtifacts` with `Effect: Deny`, `Action: s3:*` and `Resource: arn:aws:s3:::${ArtifactBucket}/rayito/*`; no `s3:DeleteObject`, no other resource, no change to the build role or the caller policy. The default persistence prefix SHALL NOT be `rayito` nor any value whose first path segment is `rayito`, because IAM's `*` crosses `/` and the image artifacts live under `<ArtifactBucket>/rayito/images/*`, so an overlapping namespace would let sandbox code that reads the execution role through IMDS overwrite the zip the next image is built from. The template SHALL stay `cfn-lint` clean and accepted by `validate-template`, `make infra-lint` SHALL lint it, and `infra/README.md` SHALL document the parameters with two distinct values in its deploy command, the reason the two namespaces must not overlap, the fail-closed consequence of the `Deny` when they do, the `AbortIncompleteMultipartUpload` lifecycle rule, the SSE-S3 default and the extra `kms:*` needed for SSE-KMS, and the gateway endpoint or NAT needed with a custom VPC egress connector.

#### Scenario: template validates and scopes
- **WHEN** `uvx cfn-lint==1.56.3 spike/m0/iam.yaml` and `aws cloudformation validate-template` run, and after the stack update `aws iam simulate-principal-policy` is queried for the execution role
- **THEN** both tools report nothing, `s3:PutObject` on `arn:aws:s3:::<bucket>/rayito-e2e/x/home.tar.gz` is `allowed` and on `arn:aws:s3:::<bucket>/rayito/x` is `implicitDeny`

#### Scenario: the default deployment cannot reach the artifacts
- **WHEN** the stack is rendered with its default parameters and one bucket passed as both `ArtifactBucket` and `PersistenceBucket`
- **THEN** the execution role's allowed object resource is `arn:aws:s3:::<bucket>/rayito-home/*`, which shares no key with `arn:aws:s3:::<bucket>/rayito/images/*`

#### Scenario: an overlapping prefix fails closed instead of reaching the artifacts
- **WHEN** an operator deploys with `ArtifactBucket` equal to `PersistenceBucket` and `PersistencePrefix=rayito`
- **THEN** the explicit `Deny` of `NeverTheImageArtifacts` wins over the `Allow`, so every persistence call answers `AccessDenied` and no `PutObject` reaches `rayito/images/*`

#### Scenario: the parameter pattern rejects a wildcard
- **WHEN** the template is deployed with `PersistencePrefix=*` (or a value carrying `'`, `(`, `)` or `!`)
- **THEN** CloudFormation rejects the parameter against its `AllowedPattern`, while `rayito-home`, `rayito-e2e` and `team/dev` are accepted

#### Scenario: the SDK validator is left alone
- **WHEN** a reviewer compares the template's pattern with `clients/python/src/rayito/_models.py` and `crates/rayd-core/src/persistence/keys.rs`
- **THEN** both still accept `*` as the legal S3 key character it is, and only the CloudFormation parameter is narrowed
