## ADDED Requirements

### Requirement: the sizes guard also denies image publishing
`RayitoRunAllowedSizes` SHALL deny `lambda:CreateMicrovmImage`, `lambda:UpdateMicrovmImage`, `lambda:UpdateMicrovmImageVersion` and `lambda:DeleteMicrovmImageVersion` on `*`, besides denying `lambda:RunMicrovm` outside `ImageArns`, so an identity holding it cannot rebuild an allowed image at a larger size. Its description and `SECURITY.md` T27 SHALL state the residual: an older, larger version still present under an allowed name can be launched with `imageVersion`.

#### Scenario: the publishing Deny is pinned
- **WHEN** `scripts/tests/test_sizes_guard_template.py` reads the policy
- **THEN** a `DenyImagePublishing` statement denies exactly those four actions on `*`, and no Allow grants anything but `lambda:RunMicrovm`
