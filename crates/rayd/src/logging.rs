//! JSON lines on stderr, filtered by `RAYD_LOG` (default `info`).
//!
//! Field allowlist for every event in this crate: `hook`, `rpc`, `phase`,
//! `from`, `to`, `outcome`, `reason`, `sandbox_id`, `suspend_generation`,
//! `resume_generation`, `clock_offset_ms`, `payload_chars`, `budget_ms`,
//! `grpc_port`, `hooks_port`, `version`, and for processes `pid`, `status`,
//! `exit_code`, `signal`, `seq`, `step`, `subscribers`, `live_processes`,
//! `reaped`, `duration_ms`, `errno`, `identity_switch`, `nofile_soft`,
//! `nofile_hard`, and for files `files`, `bytes`, `chunks`, `entries`,
//! `depth`, `recursive`, `events`, `live_watches`, `timeout_ms`,
//! `denied_prefixes`, and for code execution `context_id`, `execution_id`,
//! `execution_count`, `results`, `mime_types` (names only), `outcome`,
//! `kernel_pid`, `attempt`, `backoff_ms`, `warmup_ms`, `restart_ms`,
//! `restart_s`, `sidecar_restarts`, `sidecar_stderr_lines`, `contexts`,
//! `reseeded`, `failed`, `pending`, `kernels_killed`, `dropped_events`,
//! `consecutive_timeouts`, `state`, `source`, `msg` and `fields` (the
//! sidecar's own allowlisted fields as compact JSON), and for terminals
//! and suspend/resume `cols`, `rows`, `pty_devices`, `suspended_ms`,
//! `streams_closed`, `streams_pending`, `suspend_ms`, `probe_ms`,
//! `kernels_alive`, `kernels_lost`, `kernel_state_lost`, `reattach`,
//! `replayed`, `from_seq`, `op_timeout_across_resume`, and for the M6
//! hardening `hook_audit` (a message), `calls_since_run`, `anomaly`,
//! `hook_anomalies`, `stale_suspend_recovered`,
//! `resume_after_stale_recovery`, `capabilities`, `net_admin`,
//! `sys_admin`, `sys_resource`, `sys_ptrace`, `cgroup2_root`, `imds_probe`,
//! `imds_blocked`, `rule_present`, `root_reachable`, `user_probe_ran`,
//! `user_reachable`, `imds_block_unavailable`, `imds_rule_missing`, `cpu_seconds`,
//! `metadata_keys` (a count), `output_budget_bytes`, `entries_dropped`,
//! `disk_reserve`, `disk_full`, `free_bytes`, `deferred`,
//! `advisory_op_timeout`, and for the logical deadline (ADR-011)
//! `sandbox_timeout` and `timeout_forced_exit` (messages), `on_timeout`,
//! `extensions`, `overrun_ms`, `action`, `mode` and `lifecycle_phase`. Request bodies,
//! payloads, tokens, digests, commands, the shell's arguments,
//! environments, working directories, tags, stdin bytes, output bytes,
//! terminal bytes (input or output), paths, entry names, symlink targets,
//! file contents, chunks, code, cell output, mime payloads, error values,
//! traceback lines, connection files, the sidecar's raw stderr, metadata
//! keys or values, `ip` output and the IMDS probes' output are
//! never logged; error `Display` strings used as `reason` are written so
//! they never quote input.

use tracing_subscriber::EnvFilter;

pub const LOG_FILTER_ENV: &str = "RAYD_LOG";

pub fn init() -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    let filter = EnvFilter::try_from_env(LOG_FILTER_ENV).unwrap_or_else(|_| EnvFilter::new("info"));
    tracing_subscriber::fmt()
        .json()
        .with_env_filter(filter)
        .with_writer(std::io::stderr)
        .with_target(false)
        .try_init()
}
