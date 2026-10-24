## ADDED Requirements

### Requirement: The CLI exits quietly on a closed output and prints SDK warnings
When the reader closes stdout or stderr (`rayito ... | head`), both CLIs SHALL exit with code 1 without writing a traceback. Without `--verbose`, both CLIs SHALL write the SDK's warnings and errors to stderr; the TypeScript CLI SHALL pass that logger to the SDK, also to the fleet it reconnects for `rayito agent invoke|status|stop <name>`, and SHALL write a record as `<message>: <field values>`. The recipe warning `recipe_renewal_ignored` SHALL be the line `cliWarningLine` of `testdata/fleet-recipe/renewal.json` in both CLIs. INFO records SHALL still need `--verbose`.

#### Scenario: piping to head
- **WHEN** the output of a CLI command is closed by the reader
- **THEN** the CLI exits with 1 and stderr has no traceback

#### Scenario: the ignored renewal
- **WHEN** a fleet whose recipe asked for key renewal is reconnected without `--renew-gateway-keys`
- **THEN** both CLIs write `receta de la flota: recipe_renewal_ignored` to stderr
