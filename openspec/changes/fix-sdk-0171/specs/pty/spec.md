## ADDED Requirements

### Requirement: A shell that outlives a background child ends within the drain grace
When the shell of a PTY ends while a background child still holds the slave, `rayd` SHALL record the end after at most `PTY_DRAIN_GRACE` more, and when every slave closed first it SHALL record it as soon as the shell ends. The integration test SHALL bound the end by the drain grace plus a fixed slack against a child that lives far longer, and SHALL type into the shell only after it runs a typed marker.

#### Scenario: the drain grace is applied exactly
- **WHEN** `end_of_terminal` runs with paused time, an output pump that never ends and a shell that already ended
- **THEN** it returns the shell's outcome after exactly the grace

#### Scenario: a background sleep does not delay the end
- **WHEN** a shell started with `ready_bash` runs `sleep 60 &` and then `exit`
- **THEN** `PtyExited` arrives with exit code 0 before `PTY_DRAIN_GRACE + 5 s`
