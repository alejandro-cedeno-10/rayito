//! `NetworkService` (ADR-012, design D11): `UpdateNetwork` replaces the
//! whole egress policy, `GetNetwork` reports it. Both sit behind the
//! access-token layer like every RPC but `Health`, and behind the phase
//! gate of the other unary RPCs (`UNAVAILABLE` outside `Running` and
//! `Resumed`). Status messages never
//! carry an entry, an address or a credential; the proxy credentials are
//! moved straight into zeroizing buffers and never echoed.

use std::sync::Arc;

use rayd_core::network::{
    EgressEnforcement as DomainEnforcement, NetworkError, NetworkSnapshot, PolicyInput,
    UpstreamInput, Zeroizing,
};
use rayito_proto::v1::network_service_server::NetworkService;
use rayito_proto::v1::{
    EgressEnforcement, EgressProxy, GetNetworkRequest, NetworkPolicy, NetworkState,
    UpdateNetworkRequest,
};
use tonic::{Request, Response, Status};

use crate::network::NetworkManager;

pub struct NetworkGrpc {
    manager: Arc<NetworkManager>,
}

impl NetworkGrpc {
    #[must_use]
    pub fn new(manager: Arc<NetworkManager>) -> Self {
        Self { manager }
    }

    fn phase_gate(&self) -> Result<(), Status> {
        self.manager
            .phase_gate()
            .map_err(|phase| Status::unavailable(phase.to_string()))
    }
}

#[tonic::async_trait]
impl NetworkService for NetworkGrpc {
    async fn update_network(
        &self,
        request: Request<UpdateNetworkRequest>,
    ) -> Result<Response<NetworkState>, Status> {
        self.phase_gate()?;
        let input = policy_input(request.into_inner().policy.unwrap_or_default());
        match self.manager.update(input).await {
            Ok(snapshot) => Ok(Response::new(to_state(&snapshot))),
            Err(error) => Err(status_for(&error)),
        }
    }

    async fn get_network(
        &self,
        _request: Request<GetNetworkRequest>,
    ) -> Result<Response<NetworkState>, Status> {
        self.phase_gate()?;
        Ok(Response::new(to_state(&self.manager.snapshot().await)))
    }
}

fn policy_input(policy: NetworkPolicy) -> PolicyInput {
    PolicyInput {
        allow_out: policy.allow_out,
        deny_out: policy.deny_out,
        upstream: policy.egress_proxy.map(upstream_input),
    }
}

fn upstream_input(proxy: EgressProxy) -> UpstreamInput {
    UpstreamInput {
        address: proxy.address,
        username: proxy.username.map(Zeroizing::new),
        password: proxy.password.map(Zeroizing::new),
    }
}

/// The error table of design D11.
#[must_use]
pub fn status_for(error: &NetworkError) -> Status {
    match error {
        NetworkError::NoNetAdmin => Status::failed_precondition(error.to_string()),
        NetworkError::InstallFailed { .. } | NetworkError::VerifyFailed => {
            Status::internal(error.to_string())
        }
        _ => Status::invalid_argument(error.to_string()),
    }
}

fn to_state(snapshot: &NetworkSnapshot) -> NetworkState {
    NetworkState {
        allow_out: snapshot.allow_out.clone(),
        deny_out: snapshot.deny_out.clone(),
        egress_proxy_configured: snapshot.egress_proxy_configured,
        enforcement: i32::from(proto_enforcement(snapshot.enforcement)),
        local_proxy_port: u32::from(snapshot.local_proxy_port.unwrap_or(0)),
    }
}

/// The wire value `Health.egress_enforcement` and `NetworkState` carry.
#[must_use]
pub fn proto_enforcement(enforcement: DomainEnforcement) -> EgressEnforcement {
    match enforcement {
        DomainEnforcement::Unspecified => EgressEnforcement::Unspecified,
        DomainEnforcement::None => EgressEnforcement::None,
        DomainEnforcement::GuestRoutes => EgressEnforcement::GuestRoutes,
        DomainEnforcement::GuestRoutesAndProxy => EgressEnforcement::GuestRoutesAndProxy,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use rayd_core::network::EgressList;

    #[test]
    fn the_error_table_of_design_d11() {
        for (error, code) in [
            (
                NetworkError::InvalidEntry {
                    list: EgressList::AllowOut,
                    index: 3,
                },
                tonic::Code::InvalidArgument,
            ),
            (
                NetworkError::HostnameInDenyOut { index: 1 },
                tonic::Code::InvalidArgument,
            ),
            (
                NetworkError::TooManyEntries {
                    list: EgressList::DenyOut,
                },
                tonic::Code::InvalidArgument,
            ),
            (NetworkError::TooManyHostnames, tonic::Code::InvalidArgument),
            (NetworkError::PolicyTooComplex, tonic::Code::InvalidArgument),
            (
                NetworkError::InvalidProxyAddress,
                tonic::Code::InvalidArgument,
            ),
            (
                NetworkError::InvalidProxyCredentials,
                tonic::Code::InvalidArgument,
            ),
            (
                NetworkError::ProxyForbiddenAddress,
                tonic::Code::InvalidArgument,
            ),
            (
                NetworkError::ProxyUnresolvable,
                tonic::Code::InvalidArgument,
            ),
            (NetworkError::NoNetAdmin, tonic::Code::FailedPrecondition),
            (
                NetworkError::InstallFailed { step: "add_rule" },
                tonic::Code::Internal,
            ),
            (NetworkError::VerifyFailed, tonic::Code::Internal),
        ] {
            let status = status_for(&error);
            assert_eq!(status.code(), code, "{error:?}");
            assert_eq!(status.message(), error.to_string());
            assert_eq!(
                error.is_invalid_argument(),
                code == tonic::Code::InvalidArgument
            );
        }
        assert_eq!(
            status_for(&NetworkError::InvalidEntry {
                list: EgressList::AllowOut,
                index: 3
            })
            .message(),
            "allow_out[3]: no es un CIDR, una IP ni un nombre de host válido"
        );
    }

    #[test]
    fn the_state_never_carries_the_proxy() {
        let state = to_state(&NetworkSnapshot {
            allow_out: vec!["api.example.com".to_owned()],
            deny_out: vec!["0.0.0.0/0".to_owned()],
            egress_proxy_configured: true,
            enforcement: DomainEnforcement::GuestRoutesAndProxy,
            local_proxy_port: Some(41000),
        });
        assert_eq!(state.allow_out, ["api.example.com"]);
        assert_eq!(state.deny_out, ["0.0.0.0/0"]);
        assert!(state.egress_proxy_configured);
        assert_eq!(
            state.enforcement,
            EgressEnforcement::GuestRoutesAndProxy as i32
        );
        assert_eq!(state.local_proxy_port, 41000);
        let idle = to_state(&NetworkSnapshot::default());
        assert_eq!(idle.local_proxy_port, 0);
        assert_eq!(idle.enforcement, EgressEnforcement::Unspecified as i32);
    }

    #[test]
    fn credentials_move_into_the_policy_input() {
        let input = policy_input(NetworkPolicy {
            allow_out: vec!["aws.amazon.com".to_owned()],
            deny_out: vec!["0.0.0.0/0".to_owned()],
            egress_proxy: Some(EgressProxy {
                address: "203.0.113.5:1080".to_owned(),
                username: Some("u-marker".to_owned()),
                password: Some("p-marker".to_owned()),
            }),
        });
        let upstream = input.upstream.as_ref().unwrap();
        assert_eq!(
            upstream.username.as_deref().map(String::as_str),
            Some("u-marker")
        );
        assert_eq!(
            upstream.password.as_deref().map(String::as_str),
            Some("p-marker")
        );
        assert!(!format!("{input:?}").contains("marker"));
    }
}
