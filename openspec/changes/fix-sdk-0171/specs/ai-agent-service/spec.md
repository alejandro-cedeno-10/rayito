## ADDED Requirements

### Requirement: The agent host drops botocore DEBUG bodies
When the agent host starts serving it SHALL set `botocore` and `urllib3` to `WARNING` together with `httpx` and `httpcore`, and SHALL install on `botocore.endpoint`, `botocore.parsers` and `botocore.auth` a filter that drops their records below `INFO`, so request and response bodies of AWS calls made by agent code never reach the agent logs even if the agent lowers those levels. Warnings and errors of botocore SHALL still be written. Installing it twice SHALL add one filter.

#### Scenario: agent code turns botocore DEBUG on
- **WHEN** agent code sets `botocore` and `botocore.parsers` to `DEBUG` and botocore logs a response body with a `SecretString`
- **THEN** no host log line contains the value, and a botocore warning is still written
