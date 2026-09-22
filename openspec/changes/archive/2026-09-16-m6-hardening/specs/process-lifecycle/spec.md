## MODIFIED Requirements

### Requirement: Resource limits are set in the child before the privilege drop
Every spawned process SHALL start with `RLIMIT_NPROC=512`, `RLIMIT_NOFILE=4096` and `RLIMIT_CORE=0` (soft and hard), set inside `pre_exec` before the privilege drop. When the platform refuses to raise a hard limit (Lambda MicroVMs: `rayd` runs as root without `CAP_SYS_RESOURCE` and inherits `RLIMIT_NOFILE` hard 1024, measured 2026-09-15), the limit SHALL be clamped to the inherited hard limit with soft == hard instead of failing the spawn. When the run payload carries `limits.cpu_seconds` (`1..=28800`), every process started by `ProcessService.Start` and every shell started by `PtyService.Create` SHALL additionally get `RLIMIT_CPU` soft = `cpu_seconds`, hard = `cpu_seconds + 5`, so a CPU-bound process is signalled `SIGXCPU` at the soft limit and `SIGKILL` 5 s later, ending with `status:"signaled"` and the signal number; the sidecar and the kernels SHALL never receive `RLIMIT_CPU`. A payload whose `limits` is out of range SHALL leave the agent token-less (200 on `/run`, never a 4xx). The SDK SHALL expose `Sandbox.create(cpu_time_limit: int | None = None)` (sync and async), validated in the client and serialized into the payload only when set.

#### Scenario: limits visible from the shell
- **WHEN** the SDK calls `commands.run("ulimit -Sn; ulimit -Hn; ulimit -u; ulimit -c")`
- **THEN** the `NOFILE` soft and hard values are equal and are `4096` or the platform's inherited hard limit (`1024` on Lambda MicroVMs), and the remaining lines are `512` and `0`

#### Scenario: CPU-bound process is killed by its CPU budget
- **WHEN** the e2e creates a sandbox with `cpu_time_limit=2` and runs `commands.run("python3 -c 'while True: pass'", timeout=30)`
- **THEN** `CommandExitException` is raised within 8 s with `exit_code` equal to `128 + 24` or `137`, `commands.run("sleep 3")` succeeds, and `run_code("sum(range(10**7))")` succeeds because the kernel is not limited

#### Scenario: sidecar spawn spec is unlimited
- **WHEN** a host test inspects the `SpawnSpec` the sidecar launcher builds
- **THEN** its `ResourceLimits.cpu_seconds` is `None` regardless of the payload

### Requirement: Ring buffer replay with Connect(from_seq)
`rayd` SHALL keep the last 1 MiB of `DataEvent` payloads per pid, bounded additionally by a sandbox-wide output budget of 128 MiB shared by every process ring, PTY ring and execution ring. `Connect{pid, from_seq:0}` SHALL deliver only new output; `from_seq:N` SHALL replay retained events with `seq >= N` followed by live events with no gap or duplicate; `N` below the oldest retained `seq` or above the next `seq` SHALL fail with `OUT_OF_RANGE` before any message. When a push would exceed the budget, the pushing ring SHALL evict its own oldest chunks until the chunk fits; if the ring is empty and the chunk still does not fit, the chunk SHALL be delivered live to subscribers but not retained (`oldest_seq` advances past it). `Connect` SHALL always start with `StartEvent{pid}`. The SDK SHALL expose `commands.connect(pid, from_seq=0)` and `CommandHandle.last_seq`, and map `OUT_OF_RANGE` to `NotFoundException`.

#### Scenario: full replay
- **WHEN** the SDK runs `h = commands.run("for i in 1 2 3; do echo $i; sleep 1; done", background=True)` and then `full = commands.connect(h.pid, from_seq=1)`
- **THEN** both `full.wait().stdout` and `h.wait().stdout` equal `"1\n2\n3\n"`

#### Scenario: discarded sequence
- **WHEN** `Connect{pid, from_seq}` names a `seq` already evicted from the ring
- **THEN** the RPC fails with `OUT_OF_RANGE` and the SDK raises `NotFoundException`

#### Scenario: budget exhausted across rings
- **WHEN** an integration test scales the budget to 256 KiB, starts three processes that each print 200 KiB and then calls `Connect(pid, from_seq: 1)` on the first
- **THEN** every live subscriber received its full 200 KiB, the replay fails with `OUT_OF_RANGE`, and the budget counter never exceeded 256 KiB

### Requirement: Terminal events are retained for 30 seconds
After a process or PTY ends, its entry SHALL remain available to `Connect` for 30 s of running time (suspended time excluded), replaying `StartEvent`/`started`, ring data per `from_seq`, and the `EndEvent`/`exited`, after which `Connect` SHALL answer `NOT_FOUND`. At most 256 ended entries SHALL be retained at once; when an entry ends while 256 are already retained, `rayd` SHALL evict the oldest ended entries first, and `Connect` on an evicted pid SHALL answer `NOT_FOUND`. While the sandbox-wide output budget is above its 96 MiB high-water mark, the reaper SHALL additionally drop ended entries oldest-first until it is below, regardless of age, releasing their bytes, and SHALL log `output_budget_bytes` and `entries_dropped` when it crosses the mark. `SendInput`, `CloseStdin`, `SendSignal`, `Resize` and `Kill` on an ended pid SHALL answer `NOT_FOUND` immediately. `List` SHALL return only live entries.

#### Scenario: connect right after exit
- **WHEN** a process exited less than 30 s ago and the SDK calls `commands.connect(pid)`
- **THEN** `wait()` returns the process's `exit_code` without raising `NotFoundException`

#### Scenario: connect after retention
- **WHEN** a process exited more than 30 s of running time ago and the reaper ran
- **THEN** `Connect` fails with `NOT_FOUND`

#### Scenario: retention spans a pause
- **WHEN** a process exits 5 s before `/suspend`, the session stays suspended for 60 s and `Connect` arrives 5 s after `/resume`
- **THEN** `Connect` replays `StartEvent`, the ring and the `EndEvent`

#### Scenario: early reap under memory pressure
- **WHEN** a host test fills a 64 KiB budget with two ended entries and one live ring, then runs the reaper
- **THEN** the oldest ended entry is dropped although less than 30 s old, the budget is below the high-water mark and the live ring is untouched
