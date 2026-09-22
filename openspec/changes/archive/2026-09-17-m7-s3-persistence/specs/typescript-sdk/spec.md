## MODIFIED Requirements

### Requirement: Sandbox lifecycle surface
`Sandbox` SHALL expose static `create(options)`, `connect(sandboxId, options)`, `list(options) → AsyncIterable<SandboxListItem>`, `kill(sandboxId)`, `getInfo(sandboxId)`, `pause(sandboxId, { wait })`, `resume(sandboxId, { wait })`, and instance `sandboxId`, `accessToken`, `endpoint`, `endpointUrl`, `region`, `info`, `resumeGeneration`, `commands`, `files`, `pty`, `kill()`, `getInfo()`, `pause({ wait = true }) → boolean`, `resume({ wait = true })`, `isRunning()`, `getHealth()`, `getHost(port)`, `getMetrics()`, `close()`, `[Symbol.asyncDispose]()` (which kills), and, for persistence, `persist` (the bound `S3Prefix` or `undefined`), `lastRestore`, `checkpointFiles(options)`, `restoreFiles(options)` and `reincarnate(options)` as specified in `sdk-persistence`. Options SHALL be camelCase with millisecond durations: `template` (or `RAYITO_TEMPLATE`), `templateVersion`, `timeoutMs` (default 3 600 000; `maximumDurationInSeconds = ceil(timeoutMs / 1000)`; above 28 800 000 → `SandboxLifetimeError`; below 1000 or non-integer → `InvalidArgumentError`), `idle` (default `{ maxIdleSeconds 300, suspendedDurationSeconds: timeout − maxIdle, autoResume true }`, `null` disables; `maxIdleSeconds ≥ 60` and `< timeout` validated), `envs`, `executionRoleArn` (none by default), `allowedPorts` (8080 always first; 9000 refused), `ingress`/`egress` (managed names or ARNs, ≤ 10), `logging` (`"disabled"` default, `"cloudwatch"` → `/rayito/<template>`, or the explicit object), `region`, `accessToken`, `readyTimeoutMs` 90 000, `requestTimeoutMs` 60 000, `reconnectTimeoutMs` 60 000, `keepOnFailure`, `controlPlane`, `client`, `transport`, `logger`, `persist` (an `S3Prefix`; requires `executionRoleArn`, else `InvalidArgumentError` before any AWS call), `persistTimeoutMs` (default 600 000). `create()` SHALL call `run-microvm`, mint the JWE, poll `Health` (0.25 s doubling to 2 s, `get-microvm` every 5 s, `TERMINATING|TERMINATED` fatal) until `agentReady && kernelReady`, then start the refresher, then bind `persist` (with `name` defaulting to the sandbox id) and, when `persist.name` was given, run `restoreFiles({ timeoutMs: persistTimeoutMs })` swallowing only `NotFoundError` (`lastRestore` stays `undefined`); any failure before readiness or during that restore SHALL close the sandbox and, unless `keepOnFailure`, terminate the VM (except `SandboxNotReadyError`, whose poll already decided). `connect()` SHALL require the access token, accept `persist` (whose `name` must be set) for binding only, refuse terminal states with `SandboxNotFoundError`, call `resume-microvm` on `SUSPENDED` only without auto-resume, and never terminate on failure. `pause()` SHALL read `get-microvm` first and return `false` on `SUSPENDING|SUSPENDED`, else mark the pending pause, call `suspend-microvm` and, with `wait`, poll until `SUSPENDED`; `resume()` SHALL clear the pending pause, call `resume-microvm` (a conflict is not an error), re-mint every JWE and, with `wait`, wait for `Health` recording the generation. `isRunning()` SHALL be one `Health` probe. `getHost(port)` SHALL return a `HostAccess` whose `toString()` is the hostname and whose `headers` are read from the store on each access without `x-aws-proxy-force-h2`. `close()` SHALL stop the refresher, every watch and every stream, close both HTTP/2 sessions, wake dormant waiters with `SandboxError`, be idempotent and never touch the VM.

#### Scenario: create against the fakes
- **WHEN** `Sandbox.create({ template, controlPlane: fakePlane, transport: fakeTransport })` runs with the fake `Health` answering `agentReady` only on the third probe and `kernelReady` on the fourth
- **THEN** the plane recorded `runMicrovm` then `createAuthToken`, four `Health` probes were made, the refresher is scheduled, and `sbx.info.sandboxId` equals the plane's id

#### Scenario: terminate on failure unless kept
- **WHEN** `Health` never answers within `readyTimeoutMs 2000` and `get-microvm` reports `RUNNING`
- **THEN** `create()` rejects with `SandboxNotReadyError` carrying `state "RUNNING"`, and the plane recorded one `terminateMicrovm`; with `keepOnFailure: true` no `terminateMicrovm` is recorded

#### Scenario: connect on a suspended sandbox
- **WHEN** `Sandbox.connect(id, { accessToken })` is called while the plane reports `SUSPENDED` with `autoResume false`, and again with `autoResume true`
- **THEN** the first call recorded `resumeMicrovm` before polling `Health`, the second recorded none

#### Scenario: pause is idempotent and marks the instance
- **WHEN** the plane reports `RUNNING` and `pause()` is called, then reports `SUSPENDED` and `pause()` is called again
- **THEN** the first returns `true` after one `suspendMicrovm` and polling to `SUSPENDED`, the second returns `false` with no `suspendMicrovm`

#### Scenario: resume re-mints and records the generation
- **WHEN** `resume()` is called against the fakes with `resumeGeneration` scripted to 1
- **THEN** the plane recorded one `resumeMicrovm` and one `createAuthToken`, and `sbx.resumeGeneration === 1` and `(await sbx.getHealth()).resumeGeneration === 1`

#### Scenario: await using kills
- **WHEN** a test does `{ await using sbx = await Sandbox.create(...) }`
- **THEN** on scope exit the plane recorded `terminateMicrovm` and `sbx.close()` ran

#### Scenario: getHost
- **WHEN** `sbx.getHost(3000)` is called with no token covering 3000
- **THEN** the plane recorded `createAuthToken` with `[{port:3000}]`, `` `https://${host}` `` equals `sbx.endpointUrl`, `host.port === 3000`, `host.headers` has `x-aws-proxy-auth` and `x-aws-proxy-port "3000"` and no `x-aws-proxy-force-h2`; `sbx.getHost(9000)` throws `InvalidArgumentError`

#### Scenario: persist requires a role and binds
- **WHEN** `Sandbox.create({ template, persist: new S3Prefix({ bucket: "b" }), controlPlane: fakePlane })` runs without `executionRoleArn`, and again with `executionRoleArn` and `persist: new S3Prefix({ bucket: "b", name: "alice" })` while the fake `Restore` answers `NotFound`
- **THEN** the first rejects with `InvalidArgumentError` and the plane recorded no `runMicrovm`; the second resolves with `sbx.persist.keyPrefix === "rayito/alice"`, one `Restore` recorded on the fake `rayd`, and `sbx.lastRestore === undefined`
