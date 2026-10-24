## ADDED Requirements

### Requirement: Template variables reach the sandbox's processes
When a template has `set_envs()` / `setEnvs()`, `Template.build()` SHALL add `__rayito_template_env.json` to the artifact with `{"envs": {...}, "version": "rayito.template-env/1"}` (the last value of a repeated key, sorted keys, indent 2, final newline, the same bytes in both SDKs) and a `COPY` of it to `/etc/rayito/template-env.json` after the template's steps; a template without `set_envs()` SHALL add neither. `rayd` SHALL read that file once at boot, before spawning anything, and SHALL give its variables to every process, PTY and kernel under the `/run` envs, which win on a repeated key; request envs SHALL stay on top. A missing, unreadable, malformed or other-version file SHALL be an empty map with a warning that names only the path and the error class. `testdata/templates/template-env.json` SHALL be the shared vector file of both SDKs and `rayd`.

#### Scenario: a template variable is visible to a command
- **WHEN** a template built with `set_envs({"MODE": "prod"})` is launched without `envs` and runs `echo $MODE`
- **THEN** the command prints `prod`

#### Scenario: the sandbox envs win
- **WHEN** the same template is launched with `envs={"MODE": "dev"}`
- **THEN** `echo $MODE` prints `dev`

#### Scenario: a template without set_envs
- **WHEN** a template without `set_envs()` is built
- **THEN** the artifact has no `__rayito_template_env.json` and its Dockerfile no `COPY` of it
