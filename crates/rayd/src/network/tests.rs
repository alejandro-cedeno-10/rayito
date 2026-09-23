//! Design D18, `rayd` side: the manager over the in-memory kernel, the
//! local proxy with a fake resolver and dialer (documentation addresses
//! mapped to local listeners, so the guard runs on realistic addresses),
//! the upstream chain against a recording SOCKS5 server, and the log
//! hygiene capture.

use std::collections::BTreeMap;
use std::io;
use std::net::{IpAddr, SocketAddr};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use rayd_core::clock::SystemClock;
use rayd_core::network::{
    ALL_TRAFFIC, EgressEnforcement, EgressPolicy, Family, NetworkError, PolicyInput, Slot,
    TargetGuard, UpstreamInput, Zeroizing,
};
use rayd_core::session::{RunHookInput, SandboxSession};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};

use super::NetworkManager;
use super::fake_kernel::FakeKernel;
use super::proxy::{Dial, LocalProxy, ProxyPolicy, ProxySeams, Resolve};

const OWN_ADDRESS: &str = "10.0.1.5";

fn ip(text: &str) -> IpAddr {
    text.parse().unwrap()
}

fn strings(entries: &[&str]) -> Vec<String> {
    entries.iter().map(|entry| (*entry).to_owned()).collect()
}

fn input(allow: &[&str], deny: &[&str]) -> PolicyInput {
    PolicyInput {
        allow_out: strings(allow),
        deny_out: strings(deny),
        upstream: None,
    }
}

/// Names to documentation addresses; anything else does not resolve.
#[derive(Default)]
struct FakeResolver {
    names: BTreeMap<String, Vec<IpAddr>>,
    queries: Mutex<Vec<String>>,
}

impl FakeResolver {
    fn with(entries: &[(&str, &str)]) -> Self {
        let mut names: BTreeMap<String, Vec<IpAddr>> = BTreeMap::new();
        for (name, address) in entries {
            names
                .entry((*name).to_owned())
                .or_default()
                .push(ip(address));
        }
        Self {
            names,
            queries: Mutex::new(Vec::new()),
        }
    }

    fn queries(&self) -> Vec<String> {
        self.queries.lock().unwrap().clone()
    }
}

#[tonic::async_trait]
impl Resolve for FakeResolver {
    async fn resolve(&self, host: &str, _port: u16) -> io::Result<Vec<IpAddr>> {
        self.queries.lock().unwrap().push(host.to_owned());
        self.names
            .get(host)
            .cloned()
            .ok_or_else(|| io::ErrorKind::NotFound.into())
    }
}

/// Documentation addresses to local listeners; anything else is refused.
#[derive(Default)]
struct FakeDialer {
    routes: Mutex<BTreeMap<SocketAddr, SocketAddr>>,
    dialed: Mutex<Vec<SocketAddr>>,
}

impl FakeDialer {
    fn route(&self, from: SocketAddr, to: SocketAddr) {
        self.routes.lock().unwrap().insert(from, to);
    }

    fn dialed(&self) -> Vec<SocketAddr> {
        self.dialed.lock().unwrap().clone()
    }
}

#[tonic::async_trait]
impl Dial for FakeDialer {
    async fn dial(&self, address: SocketAddr) -> io::Result<TcpStream> {
        self.dialed.lock().unwrap().push(address);
        let target = self.routes.lock().unwrap().get(&address).copied();
        match target {
            Some(target) => TcpStream::connect(target).await,
            None => Err(io::ErrorKind::ConnectionRefused.into()),
        }
    }
}

/// A local echo server: whatever arrives goes back, prefixed once.
async fn echo_server() -> SocketAddr {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    tokio::spawn(async move {
        while let Ok((mut stream, _)) = listener.accept().await {
            tokio::spawn(async move {
                let mut buffer = [0u8; 4096];
                while let Ok(read) = stream.read(&mut buffer).await {
                    if read == 0 || stream.write_all(&buffer[..read]).await.is_err() {
                        break;
                    }
                }
            });
        }
    });
    address
}

struct ProxyHarness {
    proxy: LocalProxy,
    resolver: Arc<FakeResolver>,
    dialer: Arc<FakeDialer>,
    echo: SocketAddr,
}

impl ProxyHarness {
    async fn start(allow: &[&str], deny: &[&str], slots: usize) -> Self {
        Self::start_with(input(allow, deny), slots).await
    }

    async fn start_with(policy: PolicyInput, slots: usize) -> Self {
        let resolver = Arc::new(FakeResolver::with(&[
            ("api.example.test", "203.0.113.10"),
            ("other.example.test", "203.0.113.20"),
            ("sneaky.example.test", "169.254.169.254"),
            ("proxy.example.test", "203.0.113.5"),
        ]));
        let dialer = Arc::new(FakeDialer::default());
        let echo = echo_server().await;
        for target in ["203.0.113.10", "203.0.113.20", "198.51.100.7"] {
            for port in [80, 443] {
                dialer.route(SocketAddr::new(ip(target), port), echo);
            }
        }
        let seams = ProxySeams {
            resolver: resolver.clone(),
            dialer: dialer.clone(),
        };
        let initial = ProxyPolicy {
            policy: EgressPolicy::parse(policy).unwrap(),
            guard: TargetGuard::new([ip(OWN_ADDRESS)]),
        };
        let proxy = LocalProxy::start_with_slots(initial, seams, slots)
            .await
            .unwrap();
        Self {
            proxy,
            resolver,
            dialer,
            echo,
        }
    }

    async fn client(&self) -> TcpStream {
        TcpStream::connect(("127.0.0.1", self.proxy.port()))
            .await
            .unwrap()
    }

    /// The response head to a `CONNECT`, with the stream left open.
    async fn connect(&self, target: &str) -> (String, TcpStream) {
        let mut client = self.client().await;
        client
            .write_all(format!("CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n").as_bytes())
            .await
            .unwrap();
        let head = read_head(&mut client).await;
        (head, client)
    }
}

async fn read_head(stream: &mut TcpStream) -> String {
    let mut head = Vec::new();
    let mut byte = [0u8; 1];
    while !head.ends_with(b"\r\n\r\n") {
        match tokio::time::timeout(Duration::from_secs(5), stream.read(&mut byte)).await {
            Ok(Ok(1)) => head.push(byte[0]),
            _ => break,
        }
    }
    String::from_utf8_lossy(&head).into_owned()
}

async fn assert_echo(stream: &mut TcpStream) {
    stream.write_all(b"ping").await.unwrap();
    let mut answer = [0u8; 4];
    tokio::time::timeout(Duration::from_secs(5), stream.read_exact(&mut answer))
        .await
        .unwrap()
        .unwrap();
    assert_eq!(&answer, b"ping");
}

#[tokio::test]
async fn an_allowed_connect_is_relayed_and_a_denied_one_is_403() {
    let harness = ProxyHarness::start(&["api.example.test"], &[ALL_TRAFFIC], 8).await;
    let (head, mut tunnel) = harness.connect("api.example.test:443").await;
    assert!(head.starts_with("HTTP/1.1 200 "), "{head}");
    assert_echo(&mut tunnel).await;
    let (denied, _) = harness.connect("other.example.test:443").await;
    assert!(denied.starts_with("HTTP/1.1 403 "), "{denied}");
    let (wrong_port, _) = harness.connect("api.example.test:8443").await;
    assert!(wrong_port.starts_with("HTTP/1.1 403 "), "{wrong_port}");
    assert_eq!(
        harness.resolver.queries(),
        ["api.example.test"],
        "a denied name is never resolved under deny-by-default"
    );
    assert_eq!(
        harness.dialer.dialed(),
        [SocketAddr::new(ip("203.0.113.10"), 443)]
    );
    let counters = harness.proxy.counters().snapshot();
    assert_eq!((counters.allowed, counters.denied_policy), (1, 2));
}

#[tokio::test]
async fn imds_loopback_hooks_and_own_addresses_are_never_dialed() {
    let harness = ProxyHarness::start(&[ALL_TRAFFIC, "example.com"], &[ALL_TRAFFIC], 8).await;
    for target in [
        "169.254.169.254:80",
        "127.0.0.1:9000",
        "[::1]:8080",
        "[fd00:ec2::254]:80",
        "10.0.1.5:8080",
        "localhost:9000",
        "sneaky.example.test:80",
    ] {
        let (head, _) = harness.connect(target).await;
        assert!(head.starts_with("HTTP/1.1 403 "), "{target}: {head}");
    }
    assert!(harness.dialer.dialed().is_empty());
    let mut client = harness.client().await;
    client
        .write_all(b"GET http://169.254.169.254/latest/meta-data/ HTTP/1.1\r\n\r\n")
        .await
        .unwrap();
    assert!(read_head(&mut client).await.starts_with("HTTP/1.1 403 "));
    let mut socks = harness.client().await;
    socks.write_all(&[5, 1, 0]).await.unwrap();
    let mut selection = [0u8; 2];
    socks.read_exact(&mut selection).await.unwrap();
    assert_eq!(selection, [5, 0]);
    socks
        .write_all(&[5, 1, 0, 1, 169, 254, 169, 254, 0, 80])
        .await
        .unwrap();
    let mut reply = [0u8; 10];
    socks.read_exact(&mut reply).await.unwrap();
    assert_eq!(reply[1], 0x02);
    assert!(harness.dialer.dialed().is_empty());
}

#[tokio::test]
async fn socks5_flows_and_reply_codes() {
    let harness = ProxyHarness::start(&["api.example.test"], &[ALL_TRAFFIC], 8).await;
    let mut allowed = harness.client().await;
    allowed.write_all(&[5, 1, 0]).await.unwrap();
    let mut selection = [0u8; 2];
    allowed.read_exact(&mut selection).await.unwrap();
    assert_eq!(selection, [5, 0]);
    let mut request = vec![5, 1, 0, 3, 16];
    request.extend_from_slice(b"api.example.test");
    request.extend_from_slice(&443u16.to_be_bytes());
    allowed.write_all(&request).await.unwrap();
    let mut reply = [0u8; 10];
    allowed.read_exact(&mut reply).await.unwrap();
    assert_eq!(reply[..2], [5, 0]);
    assert_echo(&mut allowed).await;

    let mut denied = harness.client().await;
    denied.write_all(&[5, 1, 0]).await.unwrap();
    denied.read_exact(&mut selection).await.unwrap();
    let mut request = vec![5, 1, 0, 3, 18];
    request.extend_from_slice(b"other.example.test");
    request.extend_from_slice(&443u16.to_be_bytes());
    denied.write_all(&request).await.unwrap();
    denied.read_exact(&mut reply).await.unwrap();
    assert_eq!(reply[1], 0x02);

    let mut no_method = harness.client().await;
    no_method.write_all(&[5, 1, 2]).await.unwrap();
    no_method.read_exact(&mut selection).await.unwrap();
    assert_eq!(selection, [5, 0xFF]);

    let mut bind = harness.client().await;
    bind.write_all(&[5, 1, 0]).await.unwrap();
    bind.read_exact(&mut selection).await.unwrap();
    bind.write_all(&[5, 2, 0, 1, 203, 0, 113, 10, 0, 80])
        .await
        .unwrap();
    bind.read_exact(&mut reply).await.unwrap();
    assert_eq!(reply[1], 0x07);
}

#[tokio::test]
async fn absolute_form_requests_are_forwarded_in_origin_form() {
    let harness = ProxyHarness::start(&["api.example.test"], &[ALL_TRAFFIC], 8).await;
    let mut client = harness.client().await;
    client
        .write_all(
            b"GET http://api.example.test/path?q=1 HTTP/1.1\r\nHost: api.example.test\r\nProxy-Authorization: Basic eA==\r\n\r\n",
        )
        .await
        .unwrap();
    let echoed = read_head(&mut client).await;
    assert_eq!(
        echoed,
        "GET /path?q=1 HTTP/1.1\r\nHost: api.example.test\r\nConnection: close\r\n\r\n"
    );
    let mut https = harness.client().await;
    https
        .write_all(b"GET https://api.example.test/ HTTP/1.1\r\n\r\n")
        .await
        .unwrap();
    assert!(read_head(&mut https).await.starts_with("HTTP/1.1 400 "));
    assert_eq!(harness.echo.ip(), ip("127.0.0.1"));
}

#[tokio::test]
async fn a_client_beyond_the_connection_cap_gets_503() {
    let harness = ProxyHarness::start(&["api.example.test"], &[ALL_TRAFFIC], 1).await;
    let (head, mut tunnel) = harness.connect("api.example.test:443").await;
    assert!(head.starts_with("HTTP/1.1 200 "));
    assert_echo(&mut tunnel).await;
    let (busy, _) = harness.connect("api.example.test:443").await;
    assert!(busy.starts_with("HTTP/1.1 503 "), "{busy}");
    assert_eq!(harness.proxy.counters().snapshot().rejected_busy, 1);
}

/// What the recording SOCKS5 upstream saw.
#[derive(Debug, Default, Clone)]
struct Recorded {
    methods: Vec<u8>,
    username: String,
    password_matched: bool,
    atyp: u8,
    host: String,
    port: u16,
}

async fn recording_upstream(
    expected_password: &'static str,
) -> (SocketAddr, Arc<Mutex<Vec<Recorded>>>) {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let log = Arc::new(Mutex::new(Vec::new()));
    let sink = log.clone();
    tokio::spawn(async move {
        while let Ok((mut stream, _)) = listener.accept().await {
            let sink = sink.clone();
            tokio::spawn(async move {
                let mut record = Recorded::default();
                let mut header = [0u8; 2];
                stream.read_exact(&mut header).await.unwrap();
                let mut methods = vec![0u8; usize::from(header[1])];
                stream.read_exact(&mut methods).await.unwrap();
                record.methods.clone_from(&methods);
                if methods.contains(&2) {
                    stream.write_all(&[5, 2]).await.unwrap();
                    let mut version_len = [0u8; 2];
                    stream.read_exact(&mut version_len).await.unwrap();
                    let mut user = vec![0u8; usize::from(version_len[1])];
                    stream.read_exact(&mut user).await.unwrap();
                    let mut pass_len = [0u8; 1];
                    stream.read_exact(&mut pass_len).await.unwrap();
                    let mut pass = vec![0u8; usize::from(pass_len[0])];
                    stream.read_exact(&mut pass).await.unwrap();
                    record.username = String::from_utf8(user).unwrap();
                    record.password_matched = pass == expected_password.as_bytes();
                    stream.write_all(&[1, 0]).await.unwrap();
                } else {
                    stream.write_all(&[5, 0]).await.unwrap();
                }
                let mut request = [0u8; 4];
                stream.read_exact(&mut request).await.unwrap();
                record.atyp = request[3];
                record.host = match request[3] {
                    1 => {
                        let mut octets = [0u8; 4];
                        stream.read_exact(&mut octets).await.unwrap();
                        IpAddr::from(octets).to_string()
                    }
                    3 => {
                        let mut len = [0u8; 1];
                        stream.read_exact(&mut len).await.unwrap();
                        let mut name = vec![0u8; usize::from(len[0])];
                        stream.read_exact(&mut name).await.unwrap();
                        String::from_utf8(name).unwrap()
                    }
                    _ => String::new(),
                };
                let mut port = [0u8; 2];
                stream.read_exact(&mut port).await.unwrap();
                record.port = u16::from_be_bytes(port);
                sink.lock().unwrap().push(record);
                stream
                    .write_all(&[5, 0, 0, 1, 0, 0, 0, 0, 0, 0])
                    .await
                    .unwrap();
                let mut buffer = [0u8; 1024];
                while let Ok(read) = stream.read(&mut buffer).await {
                    if read == 0 || stream.write_all(&buffer[..read]).await.is_err() {
                        break;
                    }
                }
            });
        }
    });
    (address, log)
}

#[tokio::test]
async fn the_upstream_gets_names_by_domain_checked_targets_by_ip_and_the_credentials() {
    let (upstream, recorded) = recording_upstream("p-secret").await;
    let policy = PolicyInput {
        allow_out: strings(&["api.example.test", "198.51.100.0/24"]),
        deny_out: strings(&[ALL_TRAFFIC]),
        upstream: Some(UpstreamInput {
            address: "proxy.example.test:1080".to_owned(),
            username: Some(Zeroizing::new("rayito-e2e".to_owned())),
            password: Some(Zeroizing::new("p-secret".to_owned())),
        }),
    };
    let harness = ProxyHarness::start_with(policy, 8).await;
    harness
        .dialer
        .route(SocketAddr::new(ip("203.0.113.5"), 1080), upstream);
    let (head, mut tunnel) = harness.connect("api.example.test:443").await;
    assert!(head.starts_with("HTTP/1.1 200 "), "{head}");
    assert_echo(&mut tunnel).await;
    let (head, mut by_ip) = harness.connect("198.51.100.7:443").await;
    assert!(head.starts_with("HTTP/1.1 200 "), "{head}");
    assert_echo(&mut by_ip).await;
    let (denied, _) = harness.connect("other.example.test:443").await;
    assert!(denied.starts_with("HTTP/1.1 403 "));
    let recorded = recorded.lock().unwrap().clone();
    assert_eq!(recorded.len(), 2, "{recorded:?}");
    assert_eq!(recorded[0].methods, [0, 2]);
    assert_eq!(recorded[0].username, "rayito-e2e");
    assert!(recorded[0].password_matched);
    assert_eq!(
        (
            recorded[0].atyp,
            recorded[0].host.as_str(),
            recorded[0].port
        ),
        (3, "api.example.test", 443)
    );
    assert_eq!(
        (
            recorded[1].atyp,
            recorded[1].host.as_str(),
            recorded[1].port
        ),
        (1, "198.51.100.7", 443)
    );
    assert!(
        !harness
            .resolver
            .queries()
            .contains(&"api.example.test".to_owned()),
        "name-allowed targets use the upstream's DNS"
    );
    assert!(
        harness
            .dialer
            .dialed()
            .iter()
            .all(|address| address.ip() == ip("203.0.113.5")),
        "nothing is dialed directly with an upstream"
    );
}

#[tokio::test]
async fn a_failing_upstream_never_falls_back_to_a_direct_connection() {
    let policy = PolicyInput {
        allow_out: strings(&["api.example.test"]),
        deny_out: strings(&[ALL_TRAFFIC]),
        upstream: Some(UpstreamInput {
            address: "203.0.113.99:1080".to_owned(),
            ..UpstreamInput::default()
        }),
    };
    let harness = ProxyHarness::start_with(policy, 8).await;
    let (head, _) = harness.connect("api.example.test:443").await;
    assert!(head.starts_with("HTTP/1.1 502 "), "{head}");
    assert_eq!(
        harness.dialer.dialed(),
        [SocketAddr::new(ip("203.0.113.99"), 1080)]
    );
    assert_eq!(harness.proxy.counters().snapshot().upstream_failed, 1);
}

fn session() -> Arc<SandboxSession> {
    Arc::new(SandboxSession::new(Arc::new(SystemClock::new()), "test"))
}

fn managed(ipv6: bool) -> (Arc<SandboxSession>, Arc<FakeKernel>, Arc<NetworkManager>) {
    let session = session();
    let kernel = Arc::new(FakeKernel::new(ipv6, vec![ip(OWN_ADDRESS)]));
    let seams = ProxySeams {
        resolver: Arc::new(FakeResolver::with(&[
            ("proxy.example", "203.0.113.5"),
            ("imds.example", "169.254.169.254"),
        ])),
        dialer: Arc::new(FakeDialer::default()),
    };
    let manager = NetworkManager::new(session.clone(), kernel.clone(), true, seams);
    (session, kernel, manager)
}

#[tokio::test]
async fn run_installs_verified_deny_all_and_starts_the_proxy() {
    let (session, kernel, manager) = managed(true);
    assert_eq!(session.egress_enforcement(), EgressEnforcement::None);
    assert_eq!(
        manager.enforce_deny_all_at_run().await,
        EgressEnforcement::GuestRoutes
    );
    assert_eq!(kernel.rule_priorities(Family::V4), [150]);
    assert_eq!(kernel.rule_priorities(Family::V6), [150]);
    assert!(kernel.blocked(ip("1.1.1.1")));
    assert!(kernel.blocked(ip("2606:4700:4700::1111")));
    let snapshot = manager.snapshot().await;
    assert_eq!(snapshot.deny_out, [ALL_TRAFFIC]);
    let port = snapshot.local_proxy_port.unwrap();
    assert_eq!(
        session.egress_env().get("HTTPS_PROXY").map(String::as_str),
        Some(format!("http://127.0.0.1:{port}").as_str())
    );
    assert_eq!(
        session.health().egress_enforcement,
        EgressEnforcement::GuestRoutes
    );
}

#[tokio::test]
async fn a_failing_executor_at_run_leaves_none() {
    let (session, kernel, manager) = managed(false);
    kernel.fail_everything();
    assert_eq!(
        manager.enforce_deny_all_at_run().await,
        EgressEnforcement::None
    );
    assert_eq!(session.health().egress_enforcement, EgressEnforcement::None);
}

/// A panic inside the `/run` deny-all task must not leave `Health` not
/// ready for the rest of the sandbox's life.
#[tokio::test]
async fn a_panicking_run_deny_all_still_settles_health() {
    let (session, kernel, manager) = managed(false);
    let payload = format!(
        "{{\"v\":1,\"token_sha256\":\"{}\",\"network\":{{\"enforce\":true}}}}",
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    );
    session.run(RunHookInput {
        sandbox_id: Some("mvm-test"),
        payload: Some(&payload),
    });
    assert!(session.egress_settling());
    assert!(!session.health().agent_ready);
    kernel.panic_on_local_addresses();
    assert_eq!(
        manager.enforce_deny_all_at_run().await,
        EgressEnforcement::None
    );
    assert!(!session.egress_settling());
    assert!(session.health().agent_ready);
    assert_eq!(session.health().egress_enforcement, EgressEnforcement::None);
}

#[tokio::test]
async fn updates_swap_slots_and_follow_each_mode() {
    let (session, kernel, manager) = managed(false);
    manager.enforce_deny_all_at_run().await;
    let routes = manager
        .update(input(&["198.51.100.7/32"], &["198.51.100.0/24"]))
        .await
        .unwrap();
    assert_eq!(routes.enforcement, EgressEnforcement::GuestRoutes);
    assert_eq!(kernel.rule_priorities(Family::V4), [Slot::B.priority()]);
    assert!(!kernel.blocked(ip("198.51.100.7")));
    assert!(kernel.blocked(ip("198.51.100.8")));
    assert!(!kernel.blocked(ip("1.1.1.1")));
    assert_eq!(kernel.table(Family::V4, Slot::A.table()), None);
    let proxy = manager
        .update(input(&["api.example.com"], &[ALL_TRAFFIC]))
        .await
        .unwrap();
    assert_eq!(proxy.enforcement, EgressEnforcement::GuestRoutesAndProxy);
    assert_eq!(kernel.rule_priorities(Family::V4), [Slot::A.priority()]);
    assert!(kernel.blocked(ip("198.51.100.7")));
    let open = manager.update(input(&[], &[])).await.unwrap();
    assert_eq!(open.enforcement, EgressEnforcement::None);
    assert!(kernel.rule_priorities(Family::V4).is_empty());
    assert!(open.local_proxy_port.is_some(), "the proxy never stops");
    assert_eq!(session.egress_enforcement(), EgressEnforcement::None);
}

#[tokio::test]
async fn a_fill_failure_keeps_the_old_policy_in_force() {
    let (session, kernel, manager) = managed(false);
    manager.enforce_deny_all_at_run().await;
    kernel.fail_step("fill_table", 1);
    let error = manager
        .update(input(&[], &["203.0.113.0/24"]))
        .await
        .unwrap_err();
    assert_eq!(error, NetworkError::InstallFailed { step: "fill_table" });
    assert_eq!(kernel.rule_priorities(Family::V4), [150]);
    assert!(kernel.blocked(ip("1.1.1.1")));
    assert_eq!(kernel.table(Family::V4, Slot::B.table()), None);
    assert_eq!(session.egress_enforcement(), EgressEnforcement::GuestRoutes);
    assert_eq!(manager.snapshot().await.deny_out, [ALL_TRAFFIC]);
}

#[tokio::test]
async fn a_commit_failure_recovers_to_deny_all_in_slot_a() {
    let (session, kernel, manager) = managed(false);
    manager
        .update(input(&[], &["203.0.113.0/24"]))
        .await
        .unwrap();
    kernel.fail_step("del_rule", 1);
    let error = manager
        .update(input(&[], &["198.51.100.0/24"]))
        .await
        .unwrap_err();
    assert_eq!(error, NetworkError::InstallFailed { step: "del_rule" });
    assert_eq!(kernel.rule_priorities(Family::V4), [150]);
    assert!(kernel.blocked(ip("1.1.1.1")), "deny-all after recovery");
    assert_eq!(manager.snapshot().await.deny_out, [ALL_TRAFFIC]);
    assert_eq!(session.egress_enforcement(), EgressEnforcement::GuestRoutes);
    assert!(kernel.executed().contains(&"add_rule:v4".to_owned()));
}

#[tokio::test]
async fn enforcing_policies_need_net_admin_and_unrestricted_ones_do_not() {
    let session = session();
    let manager = NetworkManager::unavailable(session.clone());
    assert_eq!(
        manager
            .update(input(&[], &[ALL_TRAFFIC]))
            .await
            .unwrap_err(),
        NetworkError::NoNetAdmin
    );
    let proxy_only = PolicyInput {
        upstream: Some(UpstreamInput {
            address: "203.0.113.5:1080".to_owned(),
            ..UpstreamInput::default()
        }),
        ..PolicyInput::default()
    };
    assert_eq!(
        manager.update(proxy_only).await.unwrap_err(),
        NetworkError::NoNetAdmin
    );
    let open = manager
        .update(input(&["api.example.com"], &[]))
        .await
        .unwrap();
    assert_eq!(open.enforcement, EgressEnforcement::None);
    assert_eq!(open.allow_out, ["api.example.com"]);
    assert_eq!(manager.snapshot().await.local_proxy_port, None);
    assert_eq!(
        manager
            .update(input(&["not a host"], &[]))
            .await
            .unwrap_err(),
        NetworkError::InvalidEntry {
            list: rayd_core::network::EgressList::AllowOut,
            index: 0
        }
    );
}

#[tokio::test]
async fn a_hostname_upstream_must_resolve_to_an_allowed_address() {
    let (_, _, manager) = managed(false);
    let with_upstream = |address: &str| PolicyInput {
        allow_out: strings(&["api.example.com"]),
        deny_out: strings(&[ALL_TRAFFIC]),
        upstream: Some(UpstreamInput {
            address: address.to_owned(),
            ..UpstreamInput::default()
        }),
    };
    assert_eq!(
        manager
            .update(with_upstream("nowhere.example:1080"))
            .await
            .unwrap_err(),
        NetworkError::ProxyUnresolvable
    );
    assert_eq!(
        manager
            .update(with_upstream("imds.example:80"))
            .await
            .unwrap_err(),
        NetworkError::ProxyForbiddenAddress
    );
    let chained = manager
        .update(with_upstream("proxy.example:1080"))
        .await
        .unwrap();
    assert!(chained.egress_proxy_configured);
    assert_eq!(chained.enforcement, EgressEnforcement::GuestRoutesAndProxy);
}

#[tokio::test]
async fn resume_reinstalls_a_missing_table_with_the_same_policy() {
    let (session, kernel, manager) = managed(true);
    manager
        .update(input(&["198.51.100.7/32"], &["198.51.100.0/24"]))
        .await
        .unwrap();
    assert_eq!(
        manager.reverify_after_resume().await,
        EgressEnforcement::GuestRoutes
    );
    kernel.drop_table(Family::V4, Slot::A.table());
    assert!(!kernel.blocked(ip("198.51.100.8")));
    assert_eq!(
        manager.reverify_after_resume().await,
        EgressEnforcement::GuestRoutes
    );
    assert!(kernel.blocked(ip("198.51.100.8")));
    assert!(!kernel.blocked(ip("198.51.100.7")));
    assert_eq!(manager.snapshot().await.allow_out, ["198.51.100.7/32"]);
    assert_eq!(kernel.rule_priorities(Family::V4), [Slot::A.priority()]);
    assert_eq!(session.egress_enforcement(), EgressEnforcement::GuestRoutes);
}

#[tokio::test]
async fn resume_without_any_policy_changes_nothing() {
    let (_, kernel, manager) = managed(false);
    assert_eq!(
        manager.reverify_after_resume().await,
        EgressEnforcement::None
    );
    assert!(kernel.executed().is_empty());
}

/// Collects everything the fmt subscriber writes.
#[derive(Clone, Default)]
struct CapturedLogs(Arc<Mutex<Vec<u8>>>);

impl io::Write for CapturedLogs {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        self.0.lock().unwrap().extend_from_slice(bytes);
        Ok(bytes.len())
    }

    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

impl<'a> tracing_subscriber::fmt::MakeWriter<'a> for CapturedLogs {
    type Writer = Self;

    fn make_writer(&'a self) -> Self::Writer {
        self.clone()
    }
}

/// The process-wide capture of this test binary. A thread-scoped
/// subscriber would miss events whose callsites another test thread
/// registered first (tracing caches that thread's interest), so the
/// capture is global and collects every test's egress logs, which makes
/// the hygiene check stricter, not weaker.
fn global_capture() -> &'static CapturedLogs {
    static CAPTURE: std::sync::OnceLock<CapturedLogs> = std::sync::OnceLock::new();
    CAPTURE.get_or_init(|| {
        let logs = CapturedLogs::default();
        let subscriber = tracing_subscriber::fmt()
            .with_writer(logs.clone())
            .with_max_level(tracing::Level::TRACE)
            .finish();
        tracing::subscriber::set_global_default(subscriber).unwrap();
        logs
    })
}

#[test]
fn logs_never_carry_targets_proxy_addresses_or_credentials() {
    let logs = global_capture().clone();
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap();
    runtime.block_on(async {
        let (_, _, manager) = managed(false);
        manager.enforce_deny_all_at_run().await;
        let port = manager.snapshot().await.local_proxy_port.unwrap();
        let mut client = TcpStream::connect(("127.0.0.1", port)).await.unwrap();
        client
            .write_all(b"CONNECT secret-host.example:443 HTTP/1.1\r\n\r\n")
            .await
            .unwrap();
        assert!(read_head(&mut client).await.starts_with("HTTP/1.1 403 "));
        manager
            .update(PolicyInput {
                allow_out: strings(&["secret-host.example"]),
                deny_out: strings(&[ALL_TRAFFIC]),
                upstream: Some(UpstreamInput {
                    address: "proxy.example:1080".to_owned(),
                    username: Some(Zeroizing::new("u-marker".to_owned())),
                    password: Some(Zeroizing::new("p-marker".to_owned())),
                }),
            })
            .await
            .unwrap();
    });
    let captured = String::from_utf8(logs.0.lock().unwrap().clone()).unwrap();
    assert!(captured.contains("egress_policy_applied"), "{captured}");
    for secret in [
        "secret-host",
        "proxy.example",
        "u-marker",
        "p-marker",
        "203.0.113.5",
    ] {
        assert!(!captured.contains(secret), "{secret} leaked: {captured}");
    }
}
