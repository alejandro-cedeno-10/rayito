## MODIFIED Requirements

### Requirement: the OptionalStack component templates provisions only IAM, never a bucket, image or function

`OptionalStacks.deploy("templates")`/the TS equivalent SHALL create only
the `RayitoTemplateBuilder` managed policy (`infra/templates.yaml`); it
SHALL never create, and `destroy()` SHALL never delete, an S3 bucket, a
MicroVM image or a Lambda function. The policy SHALL scope image actions
to this account's `microvm-image:*`, SHALL deny updating the images named
by `ProtectedImageNamePrefix` (`rayito-base` by default), SHALL grant
`lambda:PassNetworkConnector` only on the AWS managed connectors, SHALL
grant `Resource: "*"` only for `lambda:CreateMicrovmImage`, which AWS
authorizes on `*` rather than on the new image's ARN (AWS_API_NOTES.md
Q114), and SHALL grant S3 reads only under `rayito/` of the artifact or
base-image bucket, never `<bucket>/*`.

#### Scenario: deploying templates creates no bucket, image or function
- **WHEN** `OptionalStacks.deploy("templates", parameters={...})` is
  called
- **THEN** the stack's only resource is `AWS::IAM::ManagedPolicy`

#### Scenario: the builder cannot overwrite rayito-base
- **WHEN** the policy's statements are read
- **THEN** a `Deny` covers `UpdateMicrovmImage` on
  `microvm-image:${ProtectedImageNamePrefix}*` (a create on an existing
  name fails, so it cannot replace a base image either)

#### Scenario: the builder cannot read checkpoints or transfers
- **WHEN** the artifact bucket is also the persistence or transfer bucket
- **THEN** no S3 statement of the policy covers keys outside `rayito/`
