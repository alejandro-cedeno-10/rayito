## ADDED Requirements

### Requirement: Local e2e sessions only terminate or delete what they created
The Python e2e session SHALL terminate at the end only the MicroVMs that its own session control plane created (the ids returned by `run_microvm`), SHALL leave every other live MicroVM of the image untouched and SHALL report how many it left, without ids. MicroVMs born outside that control plane are bounded by the `maximumDurationInSeconds` guardrail of every test sandbox. The setup e2e SHALL delete the artifact zip only when its key is known and the run is shown to have uploaded it (the resumed run reported `artifactUploaded: true`, or a `HeadObject` before the interrupted run showed the key did not exist); otherwise it SHALL keep the zip and leave a note. The custom-domain e2e SHALL tag their stack with `RAYITO_E2E_TAGS` (when set; a pair that is not `K=V` fails naming only the variable) and the acceptance-run tag, which wins on a clash. The 404 of an expired route SHALL be accepted only after `STABLE_OBSERVATIONS` consecutive answers and at least the route TTL, minus a named clock-skew tolerance, since the route was registered.

#### Scenario: a MicroVM that was live before the session survives
- **WHEN** a MicroVM of the test image is live before the session starts and the session ends
- **THEN** it is still live and the session report counts it

#### Scenario: a MicroVM born outside the session control plane survives
- **WHEN** a MicroVM of the image is born during the session through a path that does not use the session control plane
- **THEN** the sweeper does not terminate it and the report counts it

#### Scenario: an interrupted setup run without a known key
- **WHEN** the interrupted setup test ends and the zip key was not known beforehand and the resumed run reported `artifactUploaded: false`
- **THEN** the zip is not deleted and the teardown notes it

#### Scenario: an early 404 is not an expiry
- **WHEN** five consecutive 404 arrive less than the route TTL minus the tolerance after the route was registered
- **THEN** the custom-domain TTL test fails
