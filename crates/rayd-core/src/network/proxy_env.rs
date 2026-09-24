//! The variables that point every child at the local forward proxy once
//! it is up (design D7): processes, PTYs and kernels get them under the
//! payload and request `envs`, so a caller can still override them (a
//! direct connection is decided by the routes anyway). The sidecar never
//! gets them: it starts before `/run` and never dials out.

use std::collections::BTreeMap;

pub const NO_PROXY_VALUE: &str = "localhost,127.0.0.1,::1";

/// `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY` and `NO_PROXY` in upper and
/// lower case; `ALL_PROXY` uses `socks5h` so names resolve at the proxy.
#[must_use]
pub fn egress_proxy_env(port: u16) -> BTreeMap<String, String> {
    let http = format!("http://127.0.0.1:{port}");
    let socks = format!("socks5h://127.0.0.1:{port}");
    let pairs = [
        ("HTTP_PROXY", http.clone()),
        ("HTTPS_PROXY", http),
        ("ALL_PROXY", socks),
        ("NO_PROXY", NO_PROXY_VALUE.to_owned()),
    ];
    pairs
        .into_iter()
        .flat_map(|(key, value)| {
            [
                (key.to_ascii_lowercase(), value.clone()),
                (key.to_owned(), value),
            ]
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_eight_keys_and_values() {
        let env = egress_proxy_env(40123);
        assert_eq!(env.len(), 8);
        for key in ["HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"] {
            assert_eq!(env[key], "http://127.0.0.1:40123", "{key}");
        }
        for key in ["ALL_PROXY", "all_proxy"] {
            assert_eq!(env[key], "socks5h://127.0.0.1:40123", "{key}");
        }
        for key in ["NO_PROXY", "no_proxy"] {
            assert_eq!(env[key], "localhost,127.0.0.1,::1", "{key}");
        }
    }
}
