//! The child environment is built from scratch: nothing `rayd` inherited from
//! the platform (`AWS_LAMBDA_MICROVM_*`, `AWS_REGION`, `RAYD_LOG`,
//! `RAYITO_ALLOW_ROOT`) reaches sandboxed code. The local egress proxy's
//! variables (ADR-012) are a layer of their own, under the payload and
//! request `envs`: overriding them is harmless, the routes still decide
//! every direct connection.

use std::collections::BTreeMap;

use super::identity::ProcessIdentity;

pub const DEFAULT_PATH: &str = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin";

/// Layers, later ones overriding earlier ones: `PATH`; `HOME`/`USER`/`LOGNAME`
/// of the identity; the egress proxy variables (empty until the proxy
/// runs); the sandbox `envs` of the `/run` payload; the request `envs`.
/// Sorted by key so the spawn is deterministic.
#[must_use]
pub fn build_child_env(
    identity: &ProcessIdentity,
    egress_env: &BTreeMap<String, String>,
    sandbox_envs: &BTreeMap<String, String>,
    request_envs: &BTreeMap<String, String>,
) -> Vec<(String, String)> {
    let mut env = BTreeMap::new();
    env.insert("PATH".to_owned(), DEFAULT_PATH.to_owned());
    env.insert("HOME".to_owned(), identity.home.clone());
    env.insert("USER".to_owned(), identity.username.clone());
    env.insert("LOGNAME".to_owned(), identity.username.clone());
    env.extend(egress_env.clone());
    env.extend(sandbox_envs.clone());
    env.extend(request_envs.clone());
    env.into_iter().collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn identity() -> ProcessIdentity {
        ProcessIdentity {
            uid: 1000,
            gid: 1000,
            groups: vec![1000],
            username: "user".to_owned(),
            home: "/home/user".to_owned(),
            shell: "/bin/bash".to_owned(),
        }
    }

    fn envs(pairs: &[(&str, &str)]) -> BTreeMap<String, String> {
        pairs
            .iter()
            .map(|(key, value)| ((*key).to_owned(), (*value).to_owned()))
            .collect()
    }

    #[test]
    fn base_variables_come_from_the_identity() {
        let env = build_child_env(
            &identity(),
            &BTreeMap::new(),
            &BTreeMap::new(),
            &BTreeMap::new(),
        );
        assert_eq!(
            env,
            vec![
                ("HOME".to_owned(), "/home/user".to_owned()),
                ("LOGNAME".to_owned(), "user".to_owned()),
                ("PATH".to_owned(), DEFAULT_PATH.to_owned()),
                ("USER".to_owned(), "user".to_owned()),
            ]
        );
    }

    #[test]
    fn request_overrides_sandbox_overrides_base() {
        let env = build_child_env(
            &identity(),
            &BTreeMap::new(),
            &envs(&[("FOO", "sandbox"), ("HOME", "/srv"), ("A", "1")]),
            &envs(&[("FOO", "bar")]),
        );
        let lookup = |key: &str| {
            env.iter()
                .find(|(name, _)| name == key)
                .map(|(_, value)| value.as_str())
        };
        assert_eq!(lookup("FOO"), Some("bar"));
        assert_eq!(lookup("HOME"), Some("/srv"));
        assert_eq!(lookup("A"), Some("1"));
        assert_eq!(lookup("USER"), Some("user"));
    }

    #[test]
    fn the_egress_layer_sits_under_the_payload_and_request_envs() {
        let egress = envs(&[
            ("HTTPS_PROXY", "http://127.0.0.1:4000"),
            ("NO_PROXY", "localhost"),
            ("HOME", "/egress"),
        ]);
        let env = build_child_env(
            &identity(),
            &egress,
            &envs(&[("NO_PROXY", "payload")]),
            &envs(&[("HOME", "/request")]),
        );
        let lookup = |key: &str| {
            env.iter()
                .find(|(name, _)| name == key)
                .map(|(_, value)| value.as_str())
        };
        assert_eq!(lookup("HTTPS_PROXY"), Some("http://127.0.0.1:4000"));
        assert_eq!(lookup("NO_PROXY"), Some("payload"));
        assert_eq!(lookup("HOME"), Some("/request"));
        let without = build_child_env(
            &identity(),
            &BTreeMap::new(),
            &envs(&[("A", "1")]),
            &BTreeMap::new(),
        );
        assert_eq!(without.len(), 5);
        assert!(without.iter().all(|(key, _)| key != "HTTPS_PROXY"));
    }

    #[test]
    fn nothing_from_the_agent_environment_is_inherited() {
        let env = build_child_env(
            &identity(),
            &BTreeMap::new(),
            &BTreeMap::new(),
            &BTreeMap::new(),
        );
        assert!(env.iter().all(|(key, _)| {
            !key.starts_with("AWS_") && key != "RAYD_LOG" && key != "RAYITO_ALLOW_ROOT"
        }));
        assert_eq!(env.len(), 4);
    }
}
