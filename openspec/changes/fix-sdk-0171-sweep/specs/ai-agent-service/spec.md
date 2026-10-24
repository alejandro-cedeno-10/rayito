## ADDED Requirements

### Requirement: The host checks the entrypoint before it is ready
When the project names an entrypoint, the agent host SHALL check it in `warm()`, before writing its ready marker: the deepagents and Strands drivers SHALL import the module and read the attribute, and the Node driver SHALL run the bridge with `--check-entry`, which imports the file and checks the named export. A missing module, file or attribute SHALL fail the host start with `entrypoint_not_found` in the failed-start marker, so the deploy fails with that code in both SDKs instead of the first invocation.

#### Scenario: a wrong --entry fails the deploy
- **WHEN** a project is deployed with an entrypoint whose attribute does not exist
- **THEN** the deploy fails with «el host del agente no pudo arrancar (entrypoint_not_found)» and no invocation is needed to see it
