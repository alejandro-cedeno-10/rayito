//! Why a policy was refused or could not be enforced. Messages are shown to
//! the SDK user through the gRPC status, so they are in Spanish and never
//! quote an entry, an address or a credential: only the list name, the
//! index and fixed step names.

use std::fmt;

use thiserror::Error;

use super::{
    EGRESS_MAX_ENTRIES_PER_LIST, EGRESS_MAX_HOSTNAME_ENTRIES, EGRESS_MAX_ROUTES_PER_FAMILY,
    EGRESS_PROXY_CREDENTIAL_MAX_BYTES,
};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EgressList {
    AllowOut,
    DenyOut,
}

impl EgressList {
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::AllowOut => "allow_out",
            Self::DenyOut => "deny_out",
        }
    }
}

impl fmt::Display for EgressList {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

#[derive(Debug, Error, Clone, PartialEq, Eq)]
pub enum NetworkError {
    #[error("{list}[{index}]: no es un CIDR, una IP ni un nombre de host válido")]
    InvalidEntry { list: EgressList, index: usize },
    #[error("deny_out[{index}]: los nombres de host no se admiten en deny_out")]
    HostnameInDenyOut { index: usize },
    #[error("{list}: admite como máximo {EGRESS_MAX_ENTRIES_PER_LIST} entradas")]
    TooManyEntries { list: EgressList },
    #[error("allow_out: admite como máximo {EGRESS_MAX_HOSTNAME_ENTRIES} nombres de host")]
    TooManyHostnames,
    #[error(
        "la política genera más de {EGRESS_MAX_ROUTES_PER_FAMILY} rutas por familia de direcciones"
    )]
    PolicyTooComplex,
    #[error("egress_proxy: la dirección debe tener la forma host:puerto o [IPv6]:puerto")]
    InvalidProxyAddress,
    #[error(
        "egress_proxy: usuario y contraseña deben tener entre 1 y {EGRESS_PROXY_CREDENTIAL_MAX_BYTES} bytes, y la contraseña exige usuario"
    )]
    InvalidProxyCredentials,
    #[error(
        "egress_proxy: la dirección apunta a un destino prohibido (loopback, IMDS, multicast o no especificada)"
    )]
    ProxyForbiddenAddress,
    #[error("egress_proxy: el nombre del proxy no resuelve")]
    ProxyUnresolvable,
    #[error("la imagen no tiene CAP_NET_ADMIN: la política de egress exige rayito-base-caps")]
    NoNetAdmin,
    #[error("egress_update_failed: {step}")]
    InstallFailed { step: &'static str },
    #[error("egress_verify_failed")]
    VerifyFailed,
}

impl NetworkError {
    /// Whether the caller sent something malformed (`INVALID_ARGUMENT`), as
    /// opposed to the guest refusing or failing to enforce it.
    #[must_use]
    pub fn is_invalid_argument(&self) -> bool {
        !matches!(
            self,
            Self::NoNetAdmin | Self::InstallFailed { .. } | Self::VerifyFailed
        )
    }
}
