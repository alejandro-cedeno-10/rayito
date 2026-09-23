//! The `ip` runner shared by the IMDS block and the egress routes: the
//! first of `IP_BINARIES` that exists, stdin fed from memory when a batch
//! is given (else `/dev/null`), stdout captured for the `show`/`get`
//! parsers, stderr captured only so the egress routes can tell a table that
//! was never created from a real failure. Neither stdout nor stderr is ever
//! logged; failures are reported as an exit code or a short fixed phrase.
//! Every invocation is bounded by `IP_COMMAND_TIMEOUT`: past it the child
//! is killed and reaped and the call fails as timed out.

use std::io;
use std::process::Stdio;
use std::time::Duration;

use rayd_core::network::IP_COMMAND_TIMEOUT;
use tokio::io::{AsyncRead, AsyncReadExt, AsyncWriteExt};
use tokio::process::{Child, Command};

/// `ip` as found on `PATH`, then where AL2023 keeps it for root.
pub const IP_BINARIES: [&str; 2] = ["ip", "/usr/sbin/ip"];

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct IpOutput {
    pub code: i32,
    pub stdout: String,
    pub stderr: String,
}

impl IpOutput {
    #[must_use]
    pub fn succeeded(&self) -> bool {
        self.code == 0
    }
}

/// `ip <args>`; `Err` only when no binary could be spawned or it outlived
/// `IP_COMMAND_TIMEOUT`.
pub async fn run_ip(args: &[&str]) -> Result<IpOutput, String> {
    run_ip_with_input(args, None).await
}

/// `ip <args>` with `input` on stdin (`ip -batch -`).
pub async fn run_ip_with_input(args: &[&str], input: Option<&[u8]>) -> Result<IpOutput, String> {
    let mut missing = None;
    for binary in IP_BINARIES {
        match run_binary(binary, args, input, IP_COMMAND_TIMEOUT).await {
            Ok(output) => return Ok(output),
            Err(error) if error.kind() == io::ErrorKind::NotFound => {
                missing = Some(format!("{binary} not found"));
            }
            Err(error) if error.kind() == io::ErrorKind::TimedOut => {
                return Err(format!("{binary} timed out"));
            }
            Err(error) => return Err(format!("{binary} spawn failed: {}", error.kind())),
        }
    }
    Err(missing.unwrap_or_else(|| "ip not found".to_owned()))
}

/// Past `budget` the child is killed and reaped, and the call fails with
/// `TimedOut`.
async fn run_binary(
    binary: &str,
    args: &[&str],
    input: Option<&[u8]>,
    budget: Duration,
) -> io::Result<IpOutput> {
    let mut command = Command::new(binary);
    command
        .args(args)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true);
    command.stdin(if input.is_some() {
        Stdio::piped()
    } else {
        Stdio::null()
    });
    let mut child = command.spawn()?;
    if let Ok(output) = tokio::time::timeout(budget, collect(&mut child, input)).await {
        return output;
    }
    let _reaped = child.kill().await;
    Err(io::Error::new(
        io::ErrorKind::TimedOut,
        "ip exceeded its budget",
    ))
}

/// Feeds stdin while stdout and stderr are drained, until the child exits.
async fn collect(child: &mut Child, input: Option<&[u8]>) -> io::Result<IpOutput> {
    let stdin = child.stdin.take();
    let stdout = child.stdout.take();
    let stderr = child.stderr.take();
    let feed = async move {
        if let (Some(bytes), Some(mut stdin)) = (input, stdin) {
            stdin.write_all(bytes).await?;
            stdin.shutdown().await?;
        }
        Ok::<(), io::Error>(())
    };
    let (status, stdout, stderr, ()) =
        tokio::try_join!(child.wait(), drain(stdout), drain(stderr), feed)?;
    Ok(IpOutput {
        code: status.code().unwrap_or(-1),
        stdout: String::from_utf8_lossy(&stdout).into_owned(),
        stderr: String::from_utf8_lossy(&stderr).into_owned(),
    })
}

async fn drain(pipe: Option<impl AsyncRead + Unpin>) -> io::Result<Vec<u8>> {
    let mut bytes = Vec::new();
    if let Some(mut pipe) = pipe {
        pipe.read_to_end(&mut bytes).await?;
    }
    Ok(bytes)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn success_is_exit_zero() {
        let ok = IpOutput {
            code: 0,
            stdout: String::new(),
            stderr: String::new(),
        };
        assert!(ok.succeeded());
        assert!(
            !IpOutput {
                code: 2,
                ..ok.clone()
            }
            .succeeded()
        );
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn a_missing_binary_is_reported_without_output() {
        let result = run_binary("/nonexistent/ip-binary", &["-V"], None, IP_COMMAND_TIMEOUT).await;
        assert_eq!(
            result.map_err(|error| error.kind()),
            Err(io::ErrorKind::NotFound)
        );
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn stdin_stdout_stderr_and_the_exit_code_are_collected() {
        let output = run_binary(
            "/bin/sh",
            &["-c", "cat; echo failed >&2; exit 3"],
            Some(b"route add blackhole 10.0.0.0/8 table 101\n"),
            IP_COMMAND_TIMEOUT,
        )
        .await
        .unwrap();
        assert_eq!(output.code, 3);
        assert_eq!(output.stdout, "route add blackhole 10.0.0.0/8 table 101\n");
        assert_eq!(output.stderr, "failed\n");
    }

    /// A hung `ip` would otherwise hold the egress manager's mutex, and
    /// `Health` not ready, for as long as it hangs.
    #[cfg(target_os = "linux")]
    #[tokio::test]
    async fn a_hung_binary_is_killed_at_its_budget() {
        let directory = tempfile::tempdir().unwrap();
        let pid_file = directory.path().join("pid");
        let script = format!("echo $$ > '{}'; exec sleep 30", pid_file.display());
        let started = std::time::Instant::now();
        let result = run_binary(
            "/bin/sh",
            &["-c", &script],
            None,
            Duration::from_millis(1_500),
        )
        .await;
        assert_eq!(
            result.map_err(|error| error.kind()),
            Err(io::ErrorKind::TimedOut)
        );
        assert!(started.elapsed() < Duration::from_secs(10));
        let pid = std::fs::read_to_string(&pid_file).unwrap();
        let proc_entry = std::path::PathBuf::from(format!("/proc/{}", pid.trim()));
        assert!(!proc_entry.exists(), "the hung child was killed and reaped");
    }
}
